import asyncio
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from renewal_agent.evidence import publish
from renewal_agent.managed import ManagedWorkspace
from renewal_agent.native import NativeTransport, SyncGate
from renewal_agent.runtime import Cancellation, run_tick, run_tick_async
from native_helper import DAY, FIXTURE, KEY, MODEL, environment
from test_native_runtime import rejected


def two_due(tmp_path):
    data = json.loads(FIXTURE.read_text())
    data["renewals"][3]["deadline"] = "2026-10-26"
    path = tmp_path / "two-due.json"
    path.write_text(json.dumps(data))
    return path


@pytest.mark.parametrize(
    "fault",
    [
        "duplicate",
        "extra-draft-field",
        "boolean-index",
        "nonfinite-unknown",
        "oversize",
        "partial-body",
        "http-error",
        "non-json",
    ],
)
def test_original_http_faults_are_retained_before_actual_sdk_draft_or_business_authority(
    fault, tmp_path
):
    def mutate(body, n, request, handler, context):
        if fault == "extra-draft-field":
            value = json.loads(body["choices"][0]["message"]["content"])
            value["to"] = "other@cedar.example"
            body["choices"][0]["message"]["content"] = json.dumps(value)
        if fault == "boolean-index":
            body["choices"][0]["index"] = False
        raw = json.dumps(body).encode()
        if fault == "duplicate":
            return raw.replace(
                b'"finish_reason": "stop"',
                b'"finish_reason":"stop","finish_reason":"stop"',
            )
        if fault == "nonfinite-unknown":
            return raw[:-1] + b',"unknown":1e999}'
        if fault == "oversize":
            return b" " * (1024 * 1024 + 1)
        if fault in ("partial-body", "http-error", "non-json"):
            handler.send_response(503 if fault == "http-error" else 200)
            handler.send_header(
                "Content-Type",
                "text/html" if fault == "non-json" else "application/json",
            )
            handler.send_header(
                "Content-Length", str(len(raw) + (50 if fault == "partial-body" else 0))
            )
            handler.end_headers()
            handler.wfile.write(raw)
            return None
        return body

    with environment(tmp_path, mutate=mutate) as e:
        receipt = rejected(e["options"])
        assert len(e["requests"]) == 1 and not receipt["sdk"]
        source = e["store"].snapshot()
        assert not source["records"]["drafts"] and not source["records"]["decisions"]
        assert receipt["cleanup_confirmed"], receipt["lifecycle"]
        original = receipt["calls"][0]["response"]
        if fault in ("partial-body", "oversize"):
            assert (
                not original["bytes"]["complete"]
                and original["bytes"]["sha256"] is None
            )
        if fault == "oversize":
            assert (
                original["bytes"]["retained_bytes"] == 1024 * 1024
                and original["bytes"]["truncated"]
            )


@pytest.mark.parametrize("fault", ["unfinished", "replay"])
def test_later_native_failure_keeps_earlier_actual_queue_and_crm_decisions(
    fault, tmp_path
):
    def mutate(body, n, request, handler, context):
        if n == 2:
            if fault == "unfinished":
                body["choices"][0]["finish_reason"] = None
            else:
                body["id"] = "chatcmpl_owned_1"
        return body

    with environment(tmp_path, mutate=mutate, fixture=two_due(tmp_path)) as e:
        receipt = rejected(e["options"], "original")
        assert len(e["requests"]) == 2 and len(receipt["sdk"]) == 1
        assert len(receipt["acknowledged_effects"]) == 3
        assert len(receipt["observed_business"]["applied_decision_ids"]) == 3
        assert len(e["store"].snapshot()["business"]["outbox"]) == 1
        assert receipt["cleanup_confirmed"], receipt["lifecycle"]
        assert receipt["reported_usage"]["input"]["value"] == 20


@pytest.mark.parametrize("point", ["initial", "after-commit"])
def test_prepared_or_after_commit_recording_failure_never_returns_success(
    point, tmp_path
):
    saves = []

    def writer(path, data):
        saves.append(data["status"])
        if point == "initial" or data["acknowledged_effects"]:
            raise OSError("controlled private receipt failure")
        publish(path, data)

    with environment(tmp_path) as e:
        receipt = rejected(
            {**e["options"], "writer": writer}, "controlled private receipt failure"
        )
        assert receipt["durable_status"] == "unknown" and receipt["result"] is None
        assert receipt["cleanup_confirmed"], receipt["lifecycle"]
        if point == "initial":
            assert len(saves) == 1 and not e["requests"]
            assert not e["store"].snapshot()["records"]["operations"]
        else:
            assert len(e["requests"]) == 1 and len(receipt["acknowledged_effects"]) == 1
            assert len(e["store"].snapshot()["business"]["outbox"]) == 1
            assert (
                e["store"].snapshot()["records"]["operations"][0]["status"] == "closed"
            )


@pytest.mark.parametrize("resource", ["async-pool", "sync-pool", "source-guard"])
def test_one_attempt_actual_resource_close_failure_after_commit_is_failure(
    resource, monkeypatch, tmp_path
):
    async_close, sync_close, source_close = (
        NativeTransport.aclose,
        SyncGate.close,
        ManagedWorkspace.close,
    )
    attempts = []
    with environment(tmp_path) as e:

        async def async_failure(self):
            attempts.append("async")
            await async_close(self)
            if resource == "async-pool":
                raise OSError("controlled async close failure")

        def sync_failure(self):
            attempts.append("sync")
            sync_close(self)
            if resource == "sync-pool":
                raise OSError("controlled sync close failure")

        def source_failure(self):
            if self is e["store"]:
                return source_close(self)
            attempts.append("source")
            source_close(self)
            if resource == "source-guard":
                raise OSError("controlled source close failure")

        monkeypatch.setattr(NativeTransport, "aclose", async_failure)
        monkeypatch.setattr(SyncGate, "close", sync_failure)
        monkeypatch.setattr(ManagedWorkspace, "close", source_failure)
        receipt = rejected(e["options"], "controlled .*close failure")
        assert (
            attempts.count("async")
            == attempts.count("sync")
            == attempts.count("source")
            == 1
        )
        assert len(receipt["acknowledged_effects"]) == 4
        assert receipt["observed_business"]["workflow_complete"]
        assert receipt["status"] == "failed" and not receipt["cleanup_confirmed"]


def test_partial_actual_async_sdk_construction_closes_already_allocated_http_and_source(
    monkeypatch, tmp_path
):
    from openai import AsyncOpenAI

    def failed(self, **options):
        assert options["http_client"] is not None
        raise RuntimeError("controlled partial async SDK construction")

    with environment(tmp_path) as e:
        monkeypatch.setattr(AsyncOpenAI, "__init__", failed)
        receipt = rejected(e["options"], "controlled partial async SDK construction")
        assert (
            receipt["allocation"]["sync_client"] and receipt["allocation"]["async_http"]
        )
        assert (
            not receipt["allocation"]["async_client"]
            and not receipt["allocation"]["graph_started"]
        )
        assert not e["requests"] and not receipt["calls"]
        assert receipt["cleanup_confirmed"], receipt["lifecycle"]
        assert e["store"].snapshot()["records"]["operations"][0]["status"] == "closed"


def test_corrupt_final_source_after_commits_keeps_acknowledgements_and_scope_closes(
    monkeypatch, tmp_path
):
    close = ManagedWorkspace.close_operation
    corrupted = []

    def corrupt(self, credential):
        value = close(self, credential)
        with sqlite3.connect(self.path) as db:
            business = json.loads(
                db.execute("SELECT document FROM business").fetchone()[0]
            )
            business["renewals"][0]["payment_received"] = "false"
            db.execute("UPDATE business SET document=?", (json.dumps(business),))
        corrupted.append(self.path.read_bytes())
        return value

    with environment(tmp_path) as e:
        monkeypatch.setattr(ManagedWorkspace, "close_operation", corrupt)
        receipt = rejected(e["options"])
        assert receipt["final_source"] is None and receipt["observed_business"] is None
        assert receipt["final_source_error"]
        assert (
            receipt["lifecycle"]["scope_closed"]
            and receipt["lifecycle"]["source_guard_closed"]
        )
        assert len(receipt["acknowledged_effects"]) == 4
        assert e["store"].path.read_bytes() == corrupted[0]


def test_real_sqlite_writer_lock_reports_unconfirmed_scope_close_after_commits(
    monkeypatch, tmp_path
):
    close = ManagedWorkspace.close_operation
    held = []
    credentials = []

    def locked(self, credential):
        credentials.append(credential)
        db = sqlite3.connect(self.path, isolation_level=None)
        db.execute("BEGIN IMMEDIATE")
        held.append(db)
        return close(self, credential)

    with environment(tmp_path) as e:
        monkeypatch.setattr(ManagedWorkspace, "close_operation", locked)
        try:
            receipt = rejected(e["options"], "database is locked")
            assert len(credentials) == 1 and not receipt["lifecycle"]["scope_closed"]
            assert receipt["lifecycle"]["source_guard_closed"]
            assert (
                receipt["lifecycle"]["async_pool_closed"]
                and receipt["lifecycle"]["sync_pool_closed"]
            )
            assert len(receipt["acknowledged_effects"]) == 4
            assert not receipt["cleanup_confirmed"]
        finally:
            for db in held:
                db.rollback()
                db.close()
        assert e["store"].snapshot()["records"]["operations"][0]["status"] == "active"
        close(e["store"], credentials[0])


@pytest.mark.parametrize("kind", ["caller", "deadline", "parent"])
def test_actual_pending_native_http_is_drained_on_cancel_or_deadline_with_prior_effects(
    kind, tmp_path
):
    entered, release = threading.Event(), threading.Event()

    def mutate(body, n, request, handler, context):
        if n == 2:
            entered.set()
            assert release.wait(15)
        return body

    async def run(options):
        cancellation = Cancellation()
        task = asyncio.create_task(
            run_tick_async(
                **{
                    **options,
                    "cancellation": cancellation,
                    "timeout_seconds": 2 if kind == "deadline" else 10,
                }
            )
        )
        try:
            assert await asyncio.to_thread(entered.wait, 7)
            if kind == "caller":
                cancellation.cancel("caller cancelled pending renewal")
            elif kind == "parent":
                task.cancel()
            try:
                await task
            except BaseException as error:
                return error.evidence
            raise AssertionError("pending cancelled native call succeeded")
        finally:
            release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    with environment(tmp_path, mutate=mutate, fixture=two_due(tmp_path)) as e:
        try:
            receipt = asyncio.run(run(e["options"]))
            assert len(e["requests"]) == 2
            assert len(receipt["acknowledged_effects"]) == 3
            assert receipt["cleanup_confirmed"], receipt["lifecycle"]
            assert receipt["calls"][-1]["response"] is None
            assert receipt["reported_usage"]["input"]["known_subtotal"] == 10
            assert receipt["reported_usage"]["input"]["unknown"] == 1
            assert len(e["store"].snapshot()["business"]["outbox"]) == 1
        finally:
            release.set()


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM, signal.SIGHUP])
def test_normal_cli_signal_fences_and_drains_pending_sdk_with_actual_prior_effects(
    sig, tmp_path
):
    entered, release = threading.Event(), threading.Event()

    def mutate(body, n, request, handler, context):
        if n == 2:
            entered.set()
            assert release.wait(15)
        return body

    with environment(tmp_path, mutate=mutate, fixture=two_due(tmp_path)) as e:
        o = e["options"]
        env = {
            k: os.environ[k]
            for k in ("PATH", "HOME", "TMPDIR", "LANG")
            if k in os.environ
        }
        env["OPENAI_API_KEY"] = KEY
        command = [
            sys.executable,
            "-I",
            "-m",
            "renewal_agent.cli",
            "--db",
            str(o["database"]),
            "--today",
            DAY,
            "--model",
            MODEL,
            "--base-url",
            o["base_url"],
            "--allow-writes",
            "--intent-json",
            json.dumps(o["intent"]),
            "--output",
            str(o["output_root"]),
        ]
        child = subprocess.Popen(
            command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        try:
            assert entered.wait(7)
            child.send_signal(sig)
            stdout, stderr = child.communicate(timeout=10)
            assert child.returncode == 1, (stdout, stderr)
            result = json.loads(stdout)
            assert result["cleanup_confirmed"], result
            assert len(result["acknowledged_effects"]) == 3
            receipt = json.loads(Path(result["receipt_path"]).read_text())
            assert (
                receipt["lifecycle"]["scope_closed"]
                and receipt["lifecycle"]["source_guard_closed"]
            )
            assert len(e["store"].snapshot()["business"]["outbox"]) == 1
        finally:
            release.set()
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=5)


def test_actual_normal_cli_ignores_ambient_org_project_and_scoped_tracing_flags(
    tmp_path,
):
    with environment(tmp_path) as e:
        o = e["options"]
        env = {
            k: os.environ[k]
            for k in ("PATH", "HOME", "TMPDIR", "LANG")
            if k in os.environ
        }
        env.update(
            OPENAI_API_KEY=KEY,
            OPENAI_ORG_ID="synthetic-ambient-org",
            OPENAI_PROJECT_ID="synthetic-ambient-project",
            LANGSMITH_TRACING="true",
            LANGCHAIN_TRACING_V2="true",
            LANGSMITH_API_KEY="synthetic-unused-trace-key",
            LANGSMITH_ENDPOINT=o["base_url"].removesuffix("/v1"),
        )
        command = [
            sys.executable,
            "-I",
            "-m",
            "renewal_agent.cli",
            "--db",
            str(o["database"]),
            "--today",
            DAY,
            "--model",
            MODEL,
            "--base-url",
            o["base_url"],
            "--allow-writes",
            "--intent-json",
            json.dumps(o["intent"]),
            "--output",
            str(o["output_root"]),
        ]
        completed = subprocess.run(command, env=env, capture_output=True, timeout=12)
        assert completed.returncode == 0, (completed.stdout, completed.stderr)
        result = json.loads(completed.stdout)
        assert result["cleanup_confirmed"] and result["business"]["workflow_complete"]
        assert len(e["requests"]) == 1 and not e["errors"]
        assert (
            e["requests"][0]["organization"] is None
            and e["requests"][0]["project"] is None
        )


def test_precancel_prepares_failure_before_source_or_sdk_allocation(tmp_path):
    with environment(tmp_path) as e:
        cancellation = Cancellation()
        cancellation.cancel("already cancelled")
        receipt = rejected(
            {**e["options"], "cancellation": cancellation}, "already cancelled"
        )
        assert not any(receipt["allocation"].values()) and not e["requests"]
        assert receipt["cleanup_confirmed"]


def test_tightened_real_request_budget_blocks_next_sdk_call_before_forwarding(
    monkeypatch, tmp_path
):
    import renewal_agent.native as native_module
    import renewal_agent.runtime as runtime_module

    monkeypatch.setattr(native_module, "MAX_REQUESTS", 1)
    monkeypatch.setattr(runtime_module, "MAX_REQUESTS", 1)
    with environment(tmp_path, fixture=two_due(tmp_path)) as e:
        receipt = rejected(e["options"])
        assert len(e["requests"]) == 1 and len(receipt["calls"]) == 1
        assert receipt["limits"]["native_requests"] == 1
        assert any("budget exhausted" in item["message"] for item in receipt["error"])
        assert len(receipt["acknowledged_effects"]) == 3
        assert receipt["cleanup_confirmed"], receipt["lifecycle"]


def test_failed_source_constructor_closes_its_actual_guard_and_never_allocates_clients(
    tmp_path,
):
    with environment(tmp_path) as e:
        with sqlite3.connect(e["store"].path) as db:
            data = json.loads(db.execute("SELECT document FROM business").fetchone()[0])
            data["renewals"][0]["payment_received"] = "false"
            db.execute("UPDATE business SET document=?", (json.dumps(data),))
        before = e["store"].path.read_bytes()
        receipt = rejected(e["options"])
        assert not e["requests"] and not receipt["calls"]
        assert receipt["allocation"]["source"]
        assert (
            not receipt["allocation"]["scope"]
            and not receipt["allocation"]["async_client"]
        )
        assert (
            receipt["lifecycle"]["source_guard_closed"] and receipt["cleanup_confirmed"]
        )
        assert e["store"].path.read_bytes() == before
