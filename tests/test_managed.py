import base64
import json
import shutil
import sqlite3
from copy import deepcopy
from pathlib import Path

import pytest

from renewal_agent.managed import ManagedWorkspace
from renewal_agent.managed_state import same, sealed
from renewal_agent.source import canonical, digest

FIXTURE = Path(__file__).parents[1] / "fixtures/starter.json"
DAY = "2026-10-15"


def seed(tmp_path):
    return ManagedWorkspace.seed(tmp_path / "state.db", FIXTURE)


def scope(store, ids=None, writable=True):
    return store.register_operation(
        digest("controlled synthetic source contract"),
        writable,
        store.inspect_intent(DAY, ids),
    )


def native(n=1, finish="stop"):
    return {
        "id": f"chatcmpl_controlled_{n}",
        "object": "chat.completion",
        "created": 1792110000,
        "model": "gpt-4.1-synthetic",
        "choices": [
            {
                "index": 0,
                "finish_reason": finish,
                "message": {
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "subject": "Northstar renewal decision",
                            "body": "Please respond with your renewal decision by 2026-10-25. No payment is assumed.",
                        }
                    ),
                    "refusal": None,
                },
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
        "unknown_original": {"preserved": True},
    }


def prepare(store, credential, renewal_id="R-101", with_draft=True):
    plan = store.observe(credential, renewal_id, "plan")
    draft = (
        store.register_draft(credential, plan["proof"], json.dumps(native()).encode())
        if with_draft
        else None
    )
    current = store.observe(credential, renewal_id, "commit")
    return plan, current, draft


def commit(store, credential, plan, current, draft=None):
    return store.commit_tick(
        credential,
        plan["value"]["id"],
        plan["observation"]["id"],
        current["proof"],
        draft["id"] if draft else None,
    )


def test_managed_atomic_queue_crm_overdue_payment_and_repeat_noop_all_replay_exactly(
    tmp_path,
):
    store = seed(tmp_path)
    run = scope(store)
    results = []
    for ident in ("R-101", "R-102", "R-103", "R-104"):
        plan, current, draft = prepare(
            store, run["credential"], ident, ident == "R-101"
        )
        results.append(commit(store, run["credential"], plan, current, draft))
    assert [r["value"]["action"] for r in results] == [
        "followup_queued",
        "crm_cancelled",
        "crm_renewed",
        "owner_review",
    ]
    source = store.snapshot()
    assert len(source["business"]["outbox"]) == len(source["business"]["tasks"]) == 1
    assert len(source["records"]["decisions"]) == 4
    assert all(d["status"] == "applied" for d in source["records"]["decisions"])
    raw = source["original"]["tables"]["business"][0]["document"]
    stored_native = source["records"]["drafts"][0]
    assert json.loads(base64.b64decode(stored_native["native_base64"]))[
        "unknown_original"
    ]["preserved"]
    store.close_operation(run["credential"])
    run = scope(store, ["R-101"])
    plan, current, _ = prepare(store, run["credential"], with_draft=False)
    result = commit(store, run["credential"], plan, current)
    assert result["value"]["action"] == "already_queued"
    assert result["decision"]["status"] == "unchanged"
    assert store.snapshot()["original"]["tables"]["business"][0]["document"] == raw
    store.close_operation(run["credential"])


@pytest.mark.parametrize(
    "updates,action",
    [
        ({"payment_received": True}, "crm_renewed"),
        ({"decision": "cancel"}, "crm_cancelled"),
        ({"decision": "renew"}, "changed_before_send"),
        ({"deadline": "2026-10-28"}, "changed_before_send"),
        ({"email": "other@cedar.example"}, "changed_before_send"),
        ({"deadline": "2026-12-01"}, "not_due"),
    ],
)
def test_actual_durable_customer_change_during_draft_is_reobserved_before_atomic_tick(
    updates, action, tmp_path
):
    store = seed(tmp_path)
    run = scope(store, ["R-101"])
    plan = store.observe(run["credential"], "R-101", "plan")
    other = ManagedWorkspace(store.path)
    other.change("R-101", 1, updates)
    # The completed old draft is retained, but does not authorize its old follow-up.
    draft = store.register_draft(
        run["credential"], plan["proof"], json.dumps(native()).encode()
    )
    current = store.observe(run["credential"], "R-101", "commit")
    result = commit(store, run["credential"], plan, current, draft)
    assert result["value"]["action"] == action
    assert store.snapshot()["business"]["outbox"] == []
    decisions = store.snapshot()["records"]["decisions"]
    assert decisions[0]["operation_id"] is None
    assert decisions[1]["operation_id"] == run["operation"]["id"]
    store.close_operation(run["credential"])


def test_readonly_wrong_scope_missing_forged_stale_consumed_and_closed_authority_cannot_queue(
    tmp_path,
):
    store = seed(tmp_path)
    run = scope(store, ["R-101"], writable=False)
    plan, current, draft = prepare(store, run["credential"])
    with pytest.raises(ValueError, match="writes_not_authorized"):
        commit(store, run["credential"], plan, current, draft)
    store.close_operation(run["credential"])
    run = scope(store, ["R-101"])
    plan, current, draft = prepare(store, run["credential"])
    with pytest.raises(ValueError, match="outside exact caller scope"):
        store.observe(run["credential"], "R-102", "commit")
    forged = {**current, "proof": {**current["proof"], "token": "0" * 64}}
    with pytest.raises(ValueError, match="genuine current"):
        commit(store, run["credential"], plan, forged, draft)
    with pytest.raises(ValueError):
        store.commit_tick(
            run["credential"], "R-101", plan["observation"]["id"], None, draft["id"]
        )
    other = ManagedWorkspace(store.path)
    other.change("R-104", 1, {"deadline": "2026-10-09"})
    with pytest.raises(ValueError, match="genuine current"):
        commit(store, run["credential"], plan, current, draft)
    current = store.observe(run["credential"], "R-101", "commit")
    assert (
        commit(store, run["credential"], plan, current, draft)["value"]["action"]
        == "followup_queued"
    )
    with pytest.raises(ValueError):
        commit(store, run["credential"], plan, current, draft)
    store.close_operation(run["credential"])
    assert store.close_operation(run["credential"])["status"] == "closed"
    with pytest.raises(ValueError, match="operation_closed"):
        store.observe(run["credential"], "R-101", "verify")


def test_only_one_model_tick_is_active_but_genuine_customer_changes_are_allowed(
    tmp_path,
):
    store = seed(tmp_path)
    run = scope(store, ["R-101"])
    other = ManagedWorkspace(store.path)
    with pytest.raises(ValueError, match="another cooperative"):
        scope(other, ["R-102"])
    other.change("R-101", 1, {"payment_received": True})
    assert store.snapshot()["business"]["renewals"][0]["payment_received"] is True
    store.close_operation(run["credential"])
    assert scope(other, ["R-102"])["operation"]["status"] == "active"


def test_current_intent_drift_and_borrowed_read_or_draft_cannot_grant_authority(
    tmp_path,
):
    store = seed(tmp_path)
    initial_intent = store.inspect_intent(DAY, ["R-101"])
    store.change("R-104", 1, {"deadline": "2026-10-09"})
    with pytest.raises(ValueError, match="current initial source"):
        store.register_operation(digest("stale intent"), True, initial_intent)
    first = scope(store, ["R-101"])
    plan, current, draft = prepare(store, first["credential"])
    store.close_operation(first["credential"])
    second = scope(store, ["R-101"])
    with pytest.raises(ValueError, match="genuine current"):
        commit(store, second["credential"], plan, current, draft)
    ownplan, owncurrent, _ = prepare(store, second["credential"], with_draft=False)
    with pytest.raises(ValueError, match="own completed"):
        commit(store, second["credential"], ownplan, owncurrent, draft)
    assert not store.snapshot()["business"]["outbox"]


@pytest.mark.parametrize(
    "fault",
    [
        "null",
        "missing",
        "length",
        "refusal",
        "unknown-draft-field",
        "duplicate",
        "boolean-index",
        "nonfinite-unknown",
    ],
)
def test_original_completion_must_be_complete_and_strict_before_durable_draft_authority(
    fault, tmp_path
):
    store = seed(tmp_path)
    run = scope(store, ["R-101"])
    plan = store.observe(run["credential"], "R-101", "plan")
    body = native()
    if fault == "null":
        body["choices"][0]["finish_reason"] = None
    elif fault == "missing":
        body["choices"][0].pop("finish_reason")
    elif fault == "length":
        body["choices"][0]["finish_reason"] = "length"
    elif fault == "refusal":
        body["choices"][0]["message"]["refusal"] = "Refused"
    elif fault == "boolean-index":
        body["choices"][0]["index"] = False
    elif fault == "unknown-draft-field":
        body["choices"][0]["message"]["content"] = json.dumps(
            {
                "subject": "Valid subject",
                "body": "A complete valid-length body.",
                "to": "other@cedar.example",
            }
        )
    raw = json.dumps(body).encode()
    if fault == "duplicate":
        raw = raw.replace(
            b'"finish_reason": "stop"', b'"finish_reason":"stop","finish_reason":"stop"'
        )
    if fault == "nonfinite-unknown":
        raw = raw[:-1] + b',"unknown_number":1e999}'
    with pytest.raises(ValueError):
        store.register_draft(run["credential"], plan["proof"], raw)
    assert not store.snapshot()["records"]["drafts"]
    assert not store.snapshot()["business"]["outbox"]


def test_precommit_fault_rolls_back_business_queue_crm_and_decision_and_read_remains_usable(
    tmp_path,
):
    store = seed(tmp_path)
    run = scope(store, ["R-101"])
    plan, current, draft = prepare(store, run["credential"])
    before = store.snapshot()

    def fail():
        raise OSError("controlled before SQLite commit")

    store.commit_guard = fail
    with pytest.raises(OSError, match="controlled before"):
        commit(store, run["credential"], plan, current, draft)
    store.commit_guard = lambda: None
    assert store.snapshot()["sha256"] == before["sha256"]
    assert (
        commit(store, run["credential"], plan, current, draft)["decision"]["status"]
        == "applied"
    )


@pytest.mark.parametrize(
    "fault",
    [
        "business",
        "trigger",
        "history",
        "read",
        "permission",
        "native",
        "schema",
        "header",
    ],
)
def test_complete_original_source_replay_and_rehashed_false_records_reject_without_repair(
    fault, tmp_path
):
    store = seed(tmp_path)
    run = scope(store, ["R-101"])
    plan, current, draft = prepare(store, run["credential"])
    commit(store, run["credential"], plan, current, draft)
    with sqlite3.connect(store.path) as db:
        if fault == "business":
            business = json.loads(
                db.execute("SELECT document FROM business").fetchone()[0]
            )
            business["renewals"][1]["account"] = "Different unrelated identity"
            db.execute("UPDATE business SET document=?", (canonical(business),))
        elif fault == "trigger":
            db.execute(
                "CREATE TRIGGER unexpected AFTER UPDATE ON business BEGIN SELECT 1; END"
            )
        elif fault == "history":
            db.execute("DELETE FROM decisions")
        elif fault == "schema":
            db.execute("CREATE TABLE extra (value TEXT)")
        elif fault == "header":
            db.execute("PRAGMA user_version=999")
        else:
            table = {
                "read": "observations",
                "permission": "operations",
                "native": "drafts",
            }[fault]
            ident, raw = db.execute(
                f"SELECT id,document FROM {table} LIMIT 1"
            ).fetchone()
            value = json.loads(raw)
            if fault == "read":
                value["value"]["payment_received"] = True
            elif fault == "permission":
                value["allow_writes"] = False
            else:
                value["value"][
                    "body"
                ] = "Different complete length but invented original native draft."
            value.pop("content_sha256")
            db.execute(
                f"UPDATE {table} SET document=? WHERE id=?",
                (canonical(sealed(value)), ident),
            )
    before = store.path.read_bytes()
    with pytest.raises((ValueError, sqlite3.Error)):
        ManagedWorkspace(store.path)
    assert store.path.read_bytes() == before
    if fault in ("business", "history", "read", "permission", "native"):
        assert store.close_operation(run["credential"])["status"] == "closed"
    else:
        with pytest.raises((ValueError, sqlite3.Error)):
            store.close_operation(run["credential"])


def test_seed_collision_alias_missing_and_copied_or_replaced_database_are_not_adopted(
    tmp_path,
):
    store = seed(tmp_path)
    with pytest.raises(FileExistsError):
        ManagedWorkspace.seed(store.path, FIXTURE)
    copy = tmp_path / "copy.db"
    shutil.copyfile(store.path, copy)
    with pytest.raises(ValueError, match="identity"):
        ManagedWorkspace(copy)
    alias = tmp_path / "alias.db"
    alias.symlink_to(store.path)
    with pytest.raises(ValueError, match="nonsymlink"):
        ManagedWorkspace(alias)
    missing = tmp_path / "missing.db"
    with pytest.raises(OSError):
        ManagedWorkspace(missing)
    assert not missing.exists()
    store.path.unlink()
    shutil.copyfile(copy, store.path)
    with pytest.raises(ValueError, match="identity"):
        store.snapshot()


def test_actual_private_model_free_cli_initializes_inspects_and_changes_without_granting_runtime_authority(
    tmp_path,
):
    import subprocess
    import sys
    import os

    db = tmp_path / "cli.db"
    env = {k: os.environ[k] for k in ("PATH", "HOME", "TMPDIR") if k in os.environ}

    def cli(*args, expected=0):
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-m",
                "renewal_agent.managed_cli",
                "--db",
                str(db),
                *args,
            ],
            env=env,
            capture_output=True,
            timeout=10,
        )
        assert result.returncode == expected, (result.stdout, result.stderr)
        return json.loads(result.stdout)

    assert cli("init")["initialized"]
    intent = cli("inspect", "--today", DAY, "--renewal-id", "R-101")
    assert intent["targets"][0]["email"] == "maya@northstar.example"
    source = cli("inspect")
    assert not source["records"]["observations"] and not source["records"]["operations"]
    actual = cli(
        "change",
        "R-101",
        "--revision",
        "1",
        "--updates-json",
        '{"payment_received":true}',
    )
    assert actual["value"]["payment_received"] is True
    assert actual["decision"]["operation_id"] is None
    assert "error" in cli(
        "change",
        "R-101",
        "--revision",
        "2",
        "--updates-json",
        '{"payment_received":true,"payment_received":false}',
        expected=2,
    )
    assert "error" in cli("init", expected=2)
    before = db.read_bytes()
    assert "error" in cli("inspect", "--renewal-id", "R-101", expected=2)
    assert db.read_bytes() == before


def test_two_real_processes_register_only_one_cooperative_tick_while_scope_is_active(
    tmp_path,
):
    import asyncio
    import os
    import sys

    store = seed(tmp_path)
    intent = store.inspect_intent(DAY, ["R-101"])
    source = """import json,sys
from renewal_agent.managed import ManagedWorkspace
from renewal_agent.source import digest
s=ManagedWorkspace(sys.argv[1]);payload=json.loads(sys.stdin.readline())
try:print(json.dumps(s.register_operation(digest('real-process-source-control'),True,payload)),flush=True)
except ValueError as e:print(json.dumps({'error':str(e)}),flush=True)
"""

    async def race():
        env = {k: os.environ[k] for k in ("PATH", "HOME", "TMPDIR") if k in os.environ}
        children = []
        try:
            for _ in range(2):
                child = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-I",
                    "-c",
                    source,
                    str(store.path),
                    env=env,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                children.append(child)
            outputs = await asyncio.gather(
                *(c.communicate((json.dumps(intent) + "\n").encode()) for c in children)
            )
            assert all(c.returncode == 0 for c in children), outputs
            return [json.loads(out) for out, _err in outputs]
        finally:
            for child in children:
                if child.returncode is None:
                    child.kill()
                    await child.wait()

    results = asyncio.run(race())
    assert sum("operation" in r for r in results) == 1
    assert (
        sum(
            r.get("error") == "another cooperative model tick is active"
            for r in results
        )
        == 1
    )
    winner = next(r for r in results if "operation" in r)
    assert store.current_operation(winner["credential"])["status"] == "active"
    store.close_operation(winner["credential"])
    assert len(store.snapshot()["records"]["operations"]) == 1
    assert not store.snapshot()["records"]["decisions"]


def test_durable_close_revokes_a_real_stopped_callers_queued_tick_before_it_resumes(
    tmp_path,
):
    import os
    import signal
    import subprocess
    import sys
    import time

    store = seed(tmp_path)
    run = scope(store, ["R-101"])
    plan, current, draft = prepare(store, run["credential"])
    code = """import json,sys
from renewal_agent.managed import ManagedWorkspace
s=ManagedWorkspace(sys.argv[1]);print('READY',flush=True)
a=json.loads(sys.stdin.readline())
try:
 print(json.dumps(s.commit_tick(a['credential'],'R-101',a['plan_id'],a['proof'],a['draft_id'])),flush=True)
except ValueError as e:
 print(json.dumps({'error':str(e)}),flush=True);sys.exit(2)
"""
    env = {k: os.environ[k] for k in ("PATH", "HOME", "TMPDIR") if k in os.environ}
    child = subprocess.Popen(
        [sys.executable, "-I", "-c", code, str(store.path)],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert child.stdout.readline() == b"READY\n"
        os.kill(child.pid, signal.SIGSTOP)
        end = time.monotonic() + 3
        while time.monotonic() < end:
            status = (
                subprocess.run(
                    ["ps", "-o", "stat=", "-p", str(child.pid)],
                    capture_output=True,
                    check=True,
                )
                .stdout.decode()
                .strip()
            )
            if status.startswith("T"):
                break
            time.sleep(0.005)
        else:
            raise AssertionError("real source caller never reached stopped state")
        payload = {
            "credential": run["credential"],
            "plan_id": plan["observation"]["id"],
            "proof": current["proof"],
            "draft_id": draft["id"],
        }
        child.stdin.write((json.dumps(payload) + "\n").encode())
        child.stdin.flush()
        assert store.close_operation(run["credential"])["status"] == "closed"
        os.kill(child.pid, signal.SIGCONT)
        stdout, stderr = child.communicate(timeout=8)
        assert child.returncode == 2, (stdout, stderr)
        assert json.loads(stdout)["error"] == "operation_closed"
        assert not store.snapshot()["business"]["outbox"]
        assert not store.snapshot()["records"]["decisions"]
    finally:
        if child.poll() is None:
            os.kill(child.pid, signal.SIGCONT)
            child.kill()
            child.communicate(timeout=5)


def test_actual_source_guard_descriptor_is_owned_and_closed_once_without_live_database_connection(
    tmp_path,
):
    import os

    store = seed(tmp_path)
    descriptor = store._guard_fd
    assert os.fstat(descriptor).st_ino == store.path.stat().st_ino
    assert store.snapshot()["business"]["renewals"]
    store.close()
    assert store.closed and store.close_attempted
    with pytest.raises(OSError):
        os.fstat(descriptor)
    with pytest.raises(ValueError, match="guard is closed"):
        store.snapshot()
    with pytest.raises(ValueError, match="already attempted"):
        store.close()
    with ManagedWorkspace(store.path) as another:
        second = another._guard_fd
        assert another.instance_id == store.instance_id
    assert another.closed
    with pytest.raises(OSError):
        os.fstat(second)


def test_real_pending_sqlite_page_growth_is_bounded_before_commit_without_recording_a_draft(
    monkeypatch, tmp_path
):
    from renewal_agent import managed_state

    store = seed(tmp_path)
    run = scope(store, ["R-101"])
    plan = store.observe(run["credential"], "R-101", "plan")
    before = store.snapshot()
    # Real SQLite allocates pending pages before physical file growth is flushed.
    # Tighten the actual policy for this controlled oversized native-record test.
    monkeypatch.setattr(
        managed_state, "MAX_DATABASE_BYTES", store.path.stat().st_size + 8192
    )
    body = native()
    body["unknown_original"]["large"] = "x" * 65536
    with pytest.raises(ValueError, match="projected managed database"):
        store.register_draft(
            run["credential"], plan["proof"], json.dumps(body).encode()
        )
    assert store.snapshot()["sha256"] == before["sha256"]
    assert not store.snapshot()["records"]["drafts"]
    store.close_operation(run["credential"])
    store.close()
