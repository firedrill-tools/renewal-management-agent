"""Actual owned HTTP clients; original completion precedes LangChain parsing."""

from __future__ import annotations

import asyncio
import os
import time
from urllib.parse import urlsplit

import httpx2

from .completion import completed
from .evidence import bytes_image, error_info, usage
from .graph import EmailDraft
from .source import canonical, parse_json

MAX_REQUESTS = 32


def configuration(model=None, api_key=None, base_url=None):
    model = os.getenv("OPENAI_MODEL") if model is None else model
    key = os.getenv("OPENAI_API_KEY") if api_key is None else api_key
    if (
        type(model) is not str
        or not 1 <= len(model) <= 200
        or any(ord(c) < 33 or ord(c) > 126 for c in model)
    ):
        raise ValueError("OPENAI_MODEL must explicitly name an enabled model")
    if (
        type(key) is not str
        or not 1 <= len(key) <= 1024
        or any(ord(c) < 33 or ord(c) > 126 for c in key)
    ):
        raise ValueError("OPENAI_API_KEY is required")
    if base_url is None:
        declared = [
            os.environ[k].rstrip("/")
            for k in ("OPENAI_BASE_URL", "OPENAI_API_BASE")
            if os.getenv(k)
        ]
        if len(set(declared)) > 1:
            raise ValueError("conflicting explicit provider origins")
        base_url = declared[0] if declared else "https://api.openai.com/v1"
    if (
        type(base_url) is not str
        or not 1 <= len(base_url) <= 1024
        or any(ord(c) < 33 or ord(c) == 127 for c in base_url)
    ):
        raise ValueError("bounded explicit provider URL required")
    parsed = urlsplit(base_url)
    if (
        not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/") != "/v1"
        or not (
            parsed.scheme == "https"
            or parsed.scheme == "http"
            and parsed.hostname in ("127.0.0.1", "localhost", "::1")
        )
    ):
        raise ValueError(
            "provider requires exact HTTPS or owned loopback HTTP /v1 origin"
        )
    _ = parsed.port
    return {
        "model": model,
        "key": key,
        "base_url": str(httpx2.URL(base_url.rstrip("/") + "/")),
    }


class SyncGate(httpx2.BaseTransport):
    """Own the actual unused sync SDK pool and deny unintended sync dispatch."""

    def __init__(self, owner):
        self.owner = owner
        self.inner = httpx2.HTTPTransport(
            trust_env=False,
            retries=0,
            limits=httpx2.Limits(max_connections=1, max_keepalive_connections=1),
        )
        self.close_attempted = self.closed = False

    def handle_request(self, request):
        self.owner.receipt.data["unexpected_sync_http"] = True
        error = ValueError("normal renewal tick requires owned async model invocation")
        self.owner.fail(error)
        raise error

    def close(self):
        if self.close_attempted:
            raise ValueError("sync native pool close already attempted")
        self.close_attempted = True
        self.inner.close()
        self.closed = True


class NativeTransport(httpx2.AsyncBaseTransport):
    def __init__(self, owner, config):
        self.owner, self.config = owner, config
        self.inner = httpx2.AsyncHTTPTransport(
            trust_env=False,
            retries=0,
            http2=False,
            limits=httpx2.Limits(max_connections=1, max_keepalive_connections=1),
        )
        self.pending = set()
        self.active = self.close_attempted = self.closed = False
        self.response_ids = set()

    async def handle_async_request(self, request):
        owner, receipt = self.owner, self.owner.receipt
        owner.guard()
        frame = owner.current_draft
        if frame is None or self.active or self.close_attempted:
            raise ValueError("parallel/closing native call or no prepared own draft")
        if len(receipt.data["calls"]) >= MAX_REQUESTS:
            raise ValueError("prepared native request budget exhausted")
        call = {
            "number": len(receipt.data["calls"]) + 1,
            "status": "prepared",
            "renewal_id": frame["renewal_id"],
            "plan_id": frame["plan"]["observation"]["id"],
            "request": None,
            "response": None,
            "reported_usage": usage(None),
            "accepted": None,
            "error": None,
        }
        receipt.data["calls"].append(call)
        receipt.save()
        self.active = True
        task = asyncio.current_task()
        self.pending.add(task)
        response = None
        try:
            phase = min(30, owner.deadline - time.monotonic())
            if phase <= 0:
                raise TimeoutError("renewal native deadline expired")
            async with asyncio.timeout(phase):
                if (
                    request.method != "POST"
                    or str(request.url) != self.config["base_url"] + "chat/completions"
                    or request.headers.get("authorization")
                    != "Bearer " + self.config["key"]
                ):
                    raise ValueError("unqualified native endpoint/method/credential")
                raw = await request.aread()
                call["request"] = bytes_image(
                    raw[: 2 * 1024 * 1024],
                    observed_bytes=len(raw),
                    truncated=len(raw) > 2 * 1024 * 1024,
                )
                if len(raw) > 2 * 1024 * 1024:
                    raise ValueError("native request exceeds2MiB retained limit")
                body = parse_json(raw)
                call["request"]["body"] = body
                if (
                    type(body) is not dict
                    or body.get("model") != self.config["model"]
                    or body.get("stream") is not False
                    or body.get("store") is not False
                    or type(body.get("n")) is not int
                    or body["n"] != 1
                    or type(body.get("max_completion_tokens")) is not int
                    or body["max_completion_tokens"] != 4096
                    or any(
                        body.get(k) is not None
                        for k in ("tools", "functions", "tool_choice", "function_call")
                    )
                ):
                    raise ValueError(
                        "native request model/output/store/stream/tool bounds differ"
                    )
                expected_format = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "EmailDraft",
                        "strict": True,
                        "schema": EmailDraft.model_json_schema(),
                    },
                }
                if canonical(body.get("response_format")) != canonical(expected_format):
                    raise ValueError("original native strict draft schema differs")
                messages = body.get("messages")
                if (
                    type(messages) is not list
                    or len(messages) != 2
                    or type(messages[0]) is not dict
                    or type(messages[1]) is not dict
                    or set(messages[0]) != {"role", "content"}
                    or set(messages[1]) != {"role", "content"}
                    or messages[0]["role"] not in ("system", "developer")
                    or messages[1]["role"] != "user"
                    or messages[0]["content"] != frame["system"]
                    or messages[1]["content"] != frame["human"]
                ):
                    raise ValueError(
                        "native messages differ from selected public original plan"
                    )
                owner.guard()
                receipt.save()
                response = await self.inner.handle_async_request(request)
                record = {
                    "http_status": response.status_code,
                    "headers": {
                        k: response.headers[k]
                        for k in (
                            "content-type",
                            "content-encoding",
                            "request-id",
                            "x-request-id",
                        )
                        if k in response.headers
                    },
                    "complete": False,
                    "bytes": None,
                    "body": None,
                }
                call["response"] = record
                parts, total, truncated = [], 0, False
                try:
                    async for chunk in response.aiter_bytes():
                        room = max(0, 1024 * 1024 - total)
                        parts.append(chunk[:room])
                        total += len(chunk)
                        if total > 1024 * 1024:
                            truncated = True
                            raise ValueError(
                                "native decoded response exceeds1MiB retained limit"
                            )
                    record["complete"] = True
                finally:
                    captured = b"".join(parts)
                    record["bytes"] = bytes_image(
                        captured,
                        complete=record["complete"],
                        observed_bytes=total,
                        truncated=truncated,
                    )
                    receipt.save()
                if (
                    response.headers.get("content-type", "")
                    .split(";", 1)[0]
                    .strip()
                    .lower()
                    != "application/json"
                ):
                    raise ValueError("native provider did not return original JSON")
                parsed = parse_json(captured)
                record["body"] = parsed
                call["reported_usage"] = usage(parsed)
                receipt.save()
                if not 200 <= response.status_code < 300:
                    raise ValueError(f"native provider HTTP{response.status_code}")
                original, value = completed(captured)
                if original["id"] in self.response_ids:
                    raise ValueError("original native completion was replayed")
                owner.guard()
                self.response_ids.add(original["id"])
                call.update(
                    status="accepted",
                    accepted={
                        "id": original["id"],
                        "model": original["model"],
                        "value": value,
                    },
                )
                frame["native"] = {
                    "raw": captured,
                    "body": original,
                    "value": value,
                    "call_number": call["number"],
                }
                receipt.save()
                owner.basic_guard()
                headers = [
                    (k, v)
                    for k, v in response.headers.multi_items()
                    if k.lower() not in ("content-encoding", "content-length")
                ]
                return httpx2.Response(
                    response.status_code,
                    headers=headers,
                    content=captured,
                    request=request,
                )
        except BaseException as error:
            call.update(status="failed", error=error_info(error))
            try:
                receipt.save()
            except BaseException:
                pass
            owner.fail(error)
            raise
        finally:
            if response is not None:
                try:
                    await response.aclose()
                except BaseException as error:
                    owner.fail(error)
            self.pending.discard(task)
            self.active = False

    async def aclose(self):
        if self.close_attempted:
            raise ValueError("async native pool close already attempted")
        self.close_attempted = True
        await self.inner.aclose()
        if self.pending:
            raise ValueError("native HTTP work not drained")
        self.closed = True
