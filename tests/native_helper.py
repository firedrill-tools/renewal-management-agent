"""Real owned local HTTP for the normal SDK; no SDK/client/model replacement."""

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from renewal_agent.managed import ManagedWorkspace

MODEL = "gpt-4.1-synthetic"
KEY = "synthetic-owned-renewal-provider-key"
DAY = "2026-10-15"
FIXTURE = Path(__file__).parents[1] / "fixtures/starter.json"


def reply(body, n):
    public = json.loads(body["messages"][1]["content"])
    row = public["renewal"]
    draft = {
        "subject": "Renewal for " + row["account"],
        "body": f"Please respond about this renewal by {row['deadline']}. Only the supplied decision and payment state are recorded.",
    }
    return {
        "id": f"chatcmpl_owned_{n}",
        "object": "chat.completion",
        "created": 1792110000,
        "model": MODEL,
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": json.dumps(draft),
                    "refusal": None,
                },
            }
        ],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 2,
            "total_tokens": 12,
            "prompt_tokens_details": {"cached_tokens": 0},
            "completion_tokens_details": {"reasoning_tokens": 0},
        },
        "unknown_original": {"preserved": True},
    }


@contextmanager
def environment(
    tmp_path,
    mutate=lambda body, n, request, handler, context: body,
    *,
    database=None,
    fixture=FIXTURE,
    ids=None,
):
    store = (
        ManagedWorkspace(database)
        if database is not None
        else ManagedWorkspace.seed(tmp_path / "state.db", fixture)
    )
    requests, errors = [], []
    context = {"store": store, "requests": requests, "errors": errors}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            try:
                request = json.loads(
                    self.rfile.read(int(self.headers["Content-Length"]))
                )
                requests.append(
                    {
                        "path": self.path,
                        "body": request,
                        "key_matches": self.headers.get("Authorization")
                        == "Bearer " + KEY,
                        "organization": self.headers.get("OpenAI-Organization"),
                        "project": self.headers.get("OpenAI-Project"),
                    }
                )
                n = len(requests)
                assert self.path == "/v1/chat/completions"
                assert requests[-1]["key_matches"]
                assert (
                    request["model"] == MODEL
                    and request["stream"] is False
                    and request["store"] is False
                )
                assert request["n"] == 1 and request["max_completion_tokens"] == 4096
                value = mutate(reply(request, n), n, request, self, context)
                if value is None:
                    return
                raw = value if isinstance(value, bytes) else json.dumps(value).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("x-request-id", f"original-owned-{n}")
                self.end_headers()
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except BaseException as error:
                errors.append(str(error))
                try:
                    raw = json.dumps({"error": {"message": str(error)}}).encode()
                    self.send_response(500)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = False
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield {
            **context,
            "options": {
                "database": store.path,
                "today": DAY,
                "renewal_ids": ids,
                "model": MODEL,
                "allow_writes": True,
                "intent": store.inspect_intent(DAY, ids),
                "api_key": KEY,
                "base_url": f"http://127.0.0.1:{server.server_port}/v1",
                "output_root": tmp_path / "receipts",
            },
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()
        store.close()
