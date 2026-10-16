import json
import os
from copy import deepcopy
from datetime import date
from pathlib import Path

import pytest

from renewal_agent.graph import EmailDraft, run_tick
from renewal_agent.source import MAX_INTEGER
from renewal_agent.workspace import Workspace

FIXTURE = Path(__file__).parents[1] / "fixtures/starter.json"


def seed(tmp_path, edit=lambda d: None):
    data = json.loads(FIXTURE.read_text())
    edit(data)
    path = tmp_path / "workspace.json"
    path.write_text(json.dumps(data))
    return path


def draft(state):
    return EmailDraft(
        subject="Renewal for " + state["snapshot"]["account"],
        body="Please respond by "
        + state["snapshot"]["deadline"]
        + ". No payment or decision is assumed.",
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("payment_received", "false"),
        ("revision", True),
        ("revision", 0),
        ("revision", MAX_INTEGER + 1),
        ("deadline", "20261025"),
        ("deadline", "2026-02-30"),
        ("decision", False),
        ("email", "person@real-domain.com"),
        ("crm_status", "renewed"),
        ("extra", "undeclared"),
    ],
)
def test_strict_renewal_source_rejects_coercion_dates_contacts_statuses_and_unknown_fields(
    field, value, tmp_path
):
    path = seed(tmp_path, lambda d: d["renewals"][0].update({field: value}))
    before = path.read_bytes()
    with pytest.raises(ValueError):
        Workspace(path)
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "raw",
    [
        b'{"renewals":[],"renewals":[],"outbox":[],"tasks":[]}',
        b"\xff",
        b'{"renewals":NaN,"outbox":[],"tasks":[]}',
    ],
)
def test_original_duplicate_nonfinite_and_invalid_utf8_are_not_normalized(
    raw, tmp_path
):
    path = tmp_path / "workspace.json"
    path.write_bytes(raw)
    with pytest.raises(ValueError):
        Workspace(path)
    assert path.read_bytes() == raw


def test_full_unrelated_table_and_duplicate_identity_are_validated(tmp_path):
    path = seed(tmp_path, lambda d: d["renewals"].append(deepcopy(d["renewals"][0])))
    with pytest.raises(ValueError, match="duplicate"):
        Workspace(path)
    path = seed(
        tmp_path, lambda d: d["tasks"].append({"key": "R-999:2026-10-10:overdue"})
    )
    with pytest.raises(ValueError):
        Workspace(path)


@pytest.mark.parametrize("alias", ["symlink", "dangling", "hardlink"])
def test_original_source_alias_never_mutates_another_owned_file(alias, tmp_path):
    outside = seed(tmp_path)
    before = outside.read_bytes()
    path = tmp_path / "alias.json"
    if alias == "hardlink":
        os.link(outside, path)
    else:
        path.symlink_to(
            outside if alias == "symlink" else tmp_path / "never-created.json"
        )
    with pytest.raises((ValueError, OSError)):
        Workspace(path)
    assert outside.read_bytes() == before
    assert not (tmp_path / "never-created.json").exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("deadline", "2026-10-28"),
        ("email", "other@cedar.example"),
        ("account", "Different account"),
        ("revision", 2),
    ],
)
def test_same_kind_changed_complete_draft_source_requires_replanning(
    field, value, tmp_path
):
    workspace = Workspace(seed(tmp_path))

    def change(_state):
        data = workspace.read()
        data["renewals"][0][field] = value
        workspace.save(data)

    result = run_tick(workspace, "2026-10-15", "R-101", draft, change)[0]
    assert result["action"] == "changed_before_send"
    assert workspace.read()["outbox"] == []
    assert workspace.get("R-101")["crm_status"] == "open"


def test_fabricated_same_key_outbox_cannot_be_imported_as_already_queued(tmp_path):
    path = seed(
        tmp_path,
        lambda d: d["outbox"].append(
            {
                "key": "R-101:2026-10-25:decision_followup",
                "to": "other@cedar.example",
                "subject": "Forged source",
                "body": "This is unrelated to the intended follow-up.",
            }
        ),
    )
    before = path.read_bytes()
    with pytest.raises(ValueError):
        Workspace(path)
    assert path.read_bytes() == before


@pytest.mark.parametrize("field", ["to", "body", "source_sha256"])
def test_original_queued_message_and_source_draft_links_reject_tampering(
    field, tmp_path
):
    path = seed(tmp_path)
    workspace = Workspace(path)
    run_tick(workspace, "2026-10-15", "R-101", draft)
    data = workspace.read()
    data["outbox"][0][field] = "wrong@cedar.example" if field == "to" else "0" * 64
    path.write_text(json.dumps(data))
    before = path.read_bytes()
    with pytest.raises(ValueError):
        Workspace(path)
    assert path.read_bytes() == before


def test_noop_and_repeated_tick_preserve_exact_source_bytes_and_safe_revision_exhaustion(
    tmp_path,
):
    path = seed(tmp_path)
    workspace = Workspace(path)
    run_tick(workspace, "2026-10-15", drafter=draft)
    before = path.read_bytes()
    inode = path.stat().st_ino
    result = run_tick(workspace, "2026-10-15", drafter=draft)
    assert result[0]["action"] == "already_queued"
    assert path.read_bytes() == before and path.stat().st_ino == inode
    workspace.change("R-101", decision="pending", payment_received=False)
    assert path.read_bytes() == before
    path = seed(tmp_path, lambda d: d["renewals"][0].update(revision=MAX_INTEGER))
    workspace = Workspace(path)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="revision_exhausted"):
        workspace.change("R-101", decision="renew")
    with pytest.raises(ValueError, match="revision_exhausted"):
        run_tick(workspace, "2026-10-15", "R-101", draft)
    assert path.read_bytes() == before


def test_direct_followup_commit_requires_complete_original_draft_binding(tmp_path):
    workspace = Workspace(seed(tmp_path))
    with pytest.raises(ValueError, match="original complete draft source"):
        workspace.commit_tick(
            "R-101",
            date(2026, 10, 15),
            "decision_followup",
            {
                "subject": "Unbound subject",
                "body": "A plausible but unbound draft has no source authority.",
            },
        )
    assert workspace.read()["outbox"] == []


@pytest.mark.parametrize("input_day", ["20261015", "2026-W42-4", "2026-02-30"])
def test_explicit_application_clock_requires_canonical_actual_day(input_day, tmp_path):
    workspace = Workspace(seed(tmp_path))
    with pytest.raises(ValueError):
        run_tick(workspace, input_day, "R-101", draft)
    assert workspace.read()["outbox"] == []
