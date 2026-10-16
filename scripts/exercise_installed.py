"""Actual installed private CLIs and normal owned SDK HTTP, without test packages."""

import importlib.util
import json
import os
import signal
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import renewal_agent

BASE = Path(sys.argv[1])
BASE.mkdir()
assert Path(renewal_agent.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
if "--production-only" in sys.argv:
    assert importlib.util.find_spec("pytest") is None
BIN = Path(sys.executable).parent
ENV = {
    k: os.environ[k]
    for k in ("PATH", "HOME", "TMPDIR", "TMP", "TEMP", "LANG", "SystemRoot", "WINDIR")
    if k in os.environ
}
KEY, MODEL, DAY = "synthetic-installed-renewal-key", "gpt-4.1-synthetic", "2026-10-15"


def execute(command, expected=0, env=ENV):
    result = subprocess.run(
        list(map(str, command)), cwd=BASE, env=env, capture_output=True, timeout=45
    )
    assert result.returncode == expected, (result.stdout[-5000:], result.stderr[-5000:])
    return json.loads(result.stdout)


manifest = execute([BIN / "renewal-cases", "list"])
assert len(manifest["cases"]) == 12
bundles = []
for case in manifest["cases"]:
    info = execute(
        [
            BIN / "renewal-cases",
            "init",
            "--case",
            case["id"],
            "--root",
            BASE / case["id"],
        ]
    )
    assert info == execute([BIN / "renewal-cases", "inspect", "--root", info["root"]])
    assert all(
        not info["source"]["records"][t]
        for t in ("operations", "observations", "drafts", "decisions")
    )
    bundles.append(info)
assert bundles[3]["metrics"] == {"renewals": 48, "outbox": 24, "tasks": 8}
assert bundles[3]["private_reference"]["prior_steps"] == 96
originals = {
    p.name: p.read_bytes() for p in Path(bundles[0]["root"]).iterdir() if p.is_file()
}
reset = execute(
    [
        BIN / "renewal-cases",
        "reset",
        "--root",
        bundles[0]["root"],
        "--new-root",
        BASE / "fresh-reset",
    ]
)
assert (
    reset["instance_id"] != bundles[0]["instance_id"]
    and reset["seed_sha256"] != bundles[0]["seed_sha256"]
)
assert reset["source"]["business"] == bundles[0]["source"]["business"]
assert {
    p.name: p.read_bytes() for p in Path(bundles[0]["root"]).iterdir() if p.is_file()
} == originals
launcher = BASE / "renewal-tick"
launcher.symlink_to(BIN / "renewal-tick")


def response(request, n):
    public = json.loads(request["messages"][1]["content"])
    row = public["renewal"]
    draft = {
        "subject": "Renewal for " + row["account"],
        "body": f"Please respond about this renewal by {row['deadline']}. Only the supplied decision and payment state are recorded.",
    }
    return {
        "id": f"chatcmpl_installed_{n}",
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
        "original_unknown": {"retained": True},
    }


proof = []
for mode in ("completed", "unfinished-after-commit", "SIGINT", "SIGTERM", "SIGHUP"):
    root = BASE / ("normal-" + mode)
    case = execute(
        [BIN / "renewal-cases", "init", "--case", "starter_decision", "--root", root]
    )
    if mode != "completed":
        execute(
            [
                BIN / "renewal-state",
                "--db",
                case["database"],
                "change",
                "R-104",
                "--revision",
                "1",
                "--updates-json",
                json.dumps({"deadline": "2026-10-24"}),
            ]
        )
    intent = execute(
        [BIN / "renewal-state", "--db", case["database"], "inspect", "--today", DAY]
    )
    requests, errors, entered, release = [], [], threading.Event(), threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            try:
                request = json.loads(
                    self.rfile.read(int(self.headers["Content-Length"]))
                )
                requests.append(request)
                n = len(requests)
                assert self.path == "/v1/chat/completions"
                assert self.headers.get("Authorization") == "Bearer " + KEY
                assert (
                    self.headers.get("OpenAI-Organization") is None
                    and self.headers.get("OpenAI-Project") is None
                )
                assert (
                    request["model"] == MODEL
                    and request["stream"] is False
                    and request["store"] is False
                )
                assert request["n"] == 1 and request["max_completion_tokens"] == 4096
                if mode.startswith("SIG") and n == 2:
                    entered.set()
                    assert release.wait(25)
                value = response(request, n)
                if mode == "unfinished-after-commit" and n == 2:
                    value["choices"][0]["finish_reason"] = None
                raw = json.dumps(value).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except BaseException as error:
                errors.append(str(error))

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = False
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    command = [
        launcher,
        "--db",
        case["database"],
        "--today",
        DAY,
        "--model",
        MODEL,
        "--base-url",
        f"http://127.0.0.1:{server.server_port}/v1",
        "--allow-writes",
        "--intent-json",
        json.dumps(intent),
        "--output",
        root / "evidence",
    ]
    child = subprocess.Popen(
        list(map(str, command)),
        cwd=BASE,
        env={**ENV, "OPENAI_API_KEY": KEY},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        if mode.startswith("SIG"):
            assert entered.wait(15)
            child.send_signal(getattr(signal, mode))
        stdout, stderr = child.communicate(timeout=30)
        assert child.returncode == (0 if mode == "completed" else 1), (
            stdout[-5000:],
            stderr[-5000:],
        )
        result = json.loads(stdout)
        receipt = json.loads(Path(result["receipt_path"]).read_text())
        assert receipt["cleanup_confirmed"], receipt["lifecycle"]
        assert len(receipt["acknowledged_effects"]) == (4 if mode == "completed" else 3)
        assert len(requests) == (1 if mode == "completed" else 2)
        source = execute([BIN / "renewal-state", "--db", case["database"], "inspect"])
        assert all(o["status"] == "closed" for o in source["records"]["operations"])
        assert len(source["business"]["outbox"]) == 1
        assert [r["crm_status"] for r in source["business"]["renewals"]][:3] == [
            "awaiting_decision",
            "cancelled",
            "renewed",
        ]
        assert len(source["business"]["tasks"]) == (1 if mode == "completed" else 0)
        assert not errors, errors
        assert KEY not in json.dumps(receipt)
        wire = json.dumps(requests)
        for private in (
            case["instance_id"],
            case["seed_sha256"],
            str(root),
            "fixture_base64",
            "definition_sha256",
        ):
            assert private not in wire
        if mode == "completed":
            assert (
                result["status"] == "completed"
                and result["business"]["workflow_complete"]
            )
            assert (
                result["usage"]["input"]["value"] == 10 and result["cost_usd"] is None
            )
        else:
            assert (
                receipt["status"] == "failed"
                and not receipt["observed_business"]["workflow_complete"]
            )
        evidence_before = Path(result["receipt_path"]).read_bytes()
        old_source = Path(case["database"]).read_bytes()
        reset = execute(
            [
                BIN / "renewal-cases",
                "reset",
                "--root",
                root,
                "--new-root",
                BASE / ("reset-" + mode),
            ]
        )
        assert reset["instance_id"] != case["instance_id"] and reset["generation"] == 2
        assert reset["source"]["business"] == case["source"]["business"]
        assert (
            Path(case["database"]).read_bytes() == old_source
            and Path(result["receipt_path"]).read_bytes() == evidence_before
        )
        proof.append(
            {
                "mode": mode,
                "requests": len(requests),
                "acknowledged_effects": len(receipt["acknowledged_effects"]),
                "cleanup_confirmed": receipt["cleanup_confirmed"],
                "preserving_reset_after_effects": True,
            }
        )
    finally:
        release.set()
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=5)
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()

seeded = execute([BIN / "renewal-state", "--db", BASE / "default.db", "init"])
assert seeded["initialized"]
source = execute([BIN / "renewal-state", "--db", BASE / "default.db", "inspect"])
assert len(source["business"]["renewals"]) == 4
missing = execute(
    [
        launcher,
        "--db",
        BASE / "default.db",
        "--today",
        DAY,
        "--model",
        MODEL,
        "--output",
        BASE / "missing-key",
    ],
    expected=1,
)
receipt = json.loads(Path(missing["receipt_path"]).read_text())
assert (
    receipt["status"] == "failed"
    and not receipt["calls"]
    and receipt["cleanup_confirmed"]
)
assert (
    not receipt["allocation"]["async_client"]
    and not receipt["allocation"]["sync_client"]
)
print(
    json.dumps(
        {
            "twelve_actual_private_cli_cases": len(bundles),
            "private_preserving_reset": True,
            "actual_installed_agent": proof,
            "installed_default_seed": True,
            "prepared_missing_key": True,
            "production_only": "--production-only" in sys.argv,
        }
    )
)
