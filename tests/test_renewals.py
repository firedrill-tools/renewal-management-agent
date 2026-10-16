import json
import shutil
from pathlib import Path

from renewal_agent.graph import EmailDraft, build_graph, run_synthetic_tick as run_tick
from renewal_agent.workspace import Workspace


FIXTURE = Path(__file__).parents[1] / "fixtures" / "starter.json"


def seeded(tmp_path):
    path = tmp_path / "workspace.json"
    shutil.copyfile(FIXTURE, path)
    return Workspace(path)


def draft(state):
    row = state["snapshot"]
    return EmailDraft(
        subject=f"Renewal for {row['account']}",
        body=f"Please respond about your renewal by {row['deadline']}. No payment is recorded yet.",
    )


def test_scheduled_tick_updates_crm_queues_one_followup_and_escalates_overdue(tmp_path):
    workspace = seeded(tmp_path)
    results = run_tick(workspace, "2026-10-15", drafter=draft)
    assert [item["action"] for item in results] == [
        "followup_queued",
        "crm_cancelled",
        "crm_renewed",
        "owner_review",
    ]
    data = workspace.read()
    assert (
        len(data["outbox"]) == 1 and data["outbox"][0]["to"] == "maya@northstar.example"
    )
    assert len(data["tasks"]) == 1 and data["tasks"][0]["renewal_id"] == "R-104"
    assert workspace.get("R-102")["crm_status"] == "cancelled"
    assert workspace.get("R-103")["crm_status"] == "renewed"
    repeat = run_tick(workspace, "2026-10-15", drafter=draft)
    assert repeat[0]["action"] == "already_queued"
    assert len(workspace.read()["outbox"]) == 1 and len(workspace.read()["tasks"]) == 1


def test_payment_arriving_after_draft_blocks_followup_and_updates_crm(tmp_path):
    workspace = seeded(tmp_path)

    def receive_payment(_state):
        workspace.change("R-101", payment_received=True)

    result = run_tick(workspace, "2026-10-15", "R-101", draft, receive_payment)[0]
    assert result["action"] == "crm_renewed"
    assert workspace.get("R-101")["crm_status"] == "renewed"
    assert workspace.read()["outbox"] == []


def test_changed_decision_or_deadline_after_draft_requires_replanning(tmp_path):
    workspace = seeded(tmp_path)

    def customer_renews(_state):
        workspace.change("R-101", decision="renew")

    first = run_tick(workspace, "2026-10-15", "R-101", draft, customer_renews)[0]
    assert first["action"] == "changed_before_send"
    assert workspace.read()["outbox"] == []
    second = run_tick(workspace, "2026-10-15", "R-101", draft)[0]
    assert second["action"] == "followup_queued"
    assert workspace.get("R-101")["crm_status"] == "awaiting_payment"


def test_deadline_clock_seam_and_cancel_during_draft(tmp_path):
    workspace = seeded(tmp_path)
    no_draft = lambda _state: (_ for _ in ()).throw(
        AssertionError("model should not run")
    )
    assert (
        run_tick(workspace, "2026-10-01", "R-101", no_draft)[0]["action"] == "not_due"
    )
    assert workspace.read()["outbox"] == []

    def cancel(_state):
        workspace.change("R-101", decision="cancel")

    assert (
        run_tick(workspace, "2026-10-15", "R-101", draft, cancel)[0]["action"]
        == "crm_cancelled"
    )
    assert workspace.read()["outbox"] == []


def test_extended_deadline_during_draft_prevents_early_followup(tmp_path):
    workspace = seeded(tmp_path)

    def extend(_state):
        workspace.change("R-101", deadline="2026-12-01")

    result = run_tick(workspace, "2026-10-15", "R-101", draft, extend)[0]
    assert result["action"] == "not_due"
    assert workspace.read()["outbox"] == []


def test_compiled_langgraph_retains_result_and_state_file(tmp_path):
    workspace = seeded(tmp_path)
    graph = build_graph(workspace, draft)
    result = graph.invoke({"renewal_id": "R-101", "today": "2026-10-15"})
    assert result["result"]["action"] == "followup_queued"
    assert (
        json.loads(workspace.path.read_text())["outbox"][0]["key"]
        == "R-101:2026-10-25:decision_followup"
    )
