"""Actual private commands, original history, current SDK and preserving reset."""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from renewal_agent import managed_state as state
from renewal_agent.cases import initialize, inspect_case, reset_case
from renewal_agent.populations import dataset, definition, fixture_bytes, replay
from renewal_agent.runtime import run_tick
from renewal_agent.source import canonical, digest
from native_helper import environment

CASES = [c["id"] for c in dataset()["cases"]]


def cli(*args):
    result = subprocess.run(
        [sys.executable, "-m", "renewal_agent.cases", *map(str, args)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("case_id", CASES)
def test_actual_private_case_cli_has_replayed_history_and_fresh_empty_authority(
    case_id, tmp_path
):
    info = cli("init", "--case", case_id, "--root", tmp_path / "case")
    inspected = cli("inspect", "--root", info["root"])
    case = definition(case_id)
    assert info == inspected
    assert Path(info["root"], "fixture.json").read_bytes() == fixture_bytes(case)
    assert replay(case) == info["source"]["business"]
    assert all(
        not info["source"]["records"][t]
        for t in ("operations", "observations", "drafts", "decisions")
    )
    assert len(info["source"]["original"]["tables"]["seed_state"]) == 1
    assert info["delivery"] == {
        "real_delivery": None,
        "prior_model": None,
        "prior_human_authentication": None,
    }
    assert info["metrics"] == dict(
        zip(
            ("renewals", "outbox", "tasks"),
            (
                case["reference"][k]
                for k in ("prior_renewals", "prior_outbox", "prior_tasks")
            ),
        )
    )
    assert info["intent"]["source_context_sha256"] == info["source"]["context_sha256"]
    assert [r["id"] for r in info["intent"]["targets"]] == case["renewal_ids"]
    assert os.stat(info["root"]).st_mode & 0o777 == 0o700
    assert all(
        os.stat(Path(info["root"], n)).st_mode & 0o777 == 0o600
        for n in ("control.json", "fixture.json", "renewals.db")
    )


@pytest.mark.parametrize("case_id", CASES)
def test_all_profiles_use_actual_owned_normal_sdk_and_preserve_real_prior_state(
    case_id, tmp_path
):
    info = initialize(tmp_path / "case", case_id)
    case = definition(case_id)
    before = deepcopy(info["source"]["business"])

    def mutate(body, n, request, handler, context):
        if n == 1:
            for action in case["during_model"]:
                current = context["store"].snapshot()["business"]
                row = next(
                    r for r in current["renewals"] if r["id"] == action["renewal_id"]
                )
                context["store"].change(row["id"], row["revision"], action["updates"])
        return body

    with environment(
        tmp_path, mutate=mutate, database=info["database"], ids=case["renewal_ids"]
    ) as e:
        options = {**e["options"], "intent": info["intent"], "today": case["today"]}
        expected = case["reference"]["expected_status"]
        if expected.startswith("failed_"):
            pattern = (
                "different original recipient"
                if expected == "failed_existing_contact"
                else "revision_exhausted"
            )
            with pytest.raises(ValueError, match=pattern) as caught:
                run_tick(**options)
            receipt = caught.value.evidence
            assert receipt["status"] == "failed" and receipt["cleanup_confirmed"]
            assert not e["store"].snapshot()["records"]["decisions"]
            assert e["store"].snapshot()["business"] == before
        else:
            result = run_tick(**options)
            receipt = json.loads(Path(result["receipt_path"]).read_text())
            assert result["status"] == expected and result["cleanup_confirmed"]
            assert result["business"]["workflow_complete"] is (expected == "completed")
            assert len(result["results"]) == len(case["renewal_ids"])
            if expected == "needs_replan":
                assert [r["action"] for r in result["results"]] == [
                    "changed_before_send",
                    "crm_cancelled",
                    "crm_renewed",
                    "owner_review",
                ]
            if case_id == "ambiguous_late_payment":
                assert result["results"][0]["action"] == "crm_renewed"
            if case_id == "boundary_last_revision":
                assert result["results"][0]["revision"] == 2**53 - 1
            if case_id == "boundary_unicode_clock":
                assert [r["action"] for r in result["results"]] == [
                    "followup_queued",
                    "not_due",
                    "followup_queued",
                    "owner_review",
                ]
        assert not e["errors"]
        assert len(e["requests"]) == case["reference"]["expected_native_requests"]
        current = e["store"].snapshot()
        assert all(o["status"] == "closed" for o in current["records"]["operations"])
        assert (
            current["business"]["outbox"][: len(before["outbox"])] == before["outbox"]
        )
        assert current["business"]["tasks"][: len(before["tasks"])] == before["tasks"]
        targets = set(case["renewal_ids"])
        assert [
            r for r in current["business"]["renewals"] if r["id"] not in targets
        ] == [r for r in before["renewals"] if r["id"] not in targets]
        wire = json.dumps(e["requests"])
        for private in (
            case_id,
            info["instance_id"],
            info["seed_sha256"],
            "definition_sha256",
            "fixture_base64",
            "private_reference",
            str(info["root"]),
        ):
            assert private not in wire
    after = inspect_case(info["root"])
    evidence = Path(info["root"], "kept-receipt.json")
    evidence.write_text(json.dumps(receipt))
    originals = {
        p.name: p.read_bytes() for p in Path(info["root"]).iterdir() if p.is_file()
    }
    reset = cli("reset", "--root", info["root"], "--new-root", tmp_path / "reset")
    assert {
        p.name: p.read_bytes() for p in Path(info["root"]).iterdir() if p.is_file()
    } == originals
    assert reset["generation"] == 2
    assert (
        reset["instance_id"] != info["instance_id"]
        and reset["seed_sha256"] != info["seed_sha256"]
    )
    assert reset["source"]["business"] == before
    assert reset["reset_of"] == {
        "root": info["root"],
        "instance_id": info["instance_id"],
        "source_sha256": after["source_sha256"],
    }
    assert all(
        not reset["source"]["records"][t]
        for t in ("operations", "observations", "drafts", "decisions")
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "seed_missing",
        "pointer_missing",
        "seed_rehashed",
        "foreign_reference",
        "control_rehashed",
        "copied",
        "locked",
        "fixture_alias",
    ],
)
def test_private_case_rejects_corrupt_foreign_stripped_or_busy_state_without_repair(
    mutation, tmp_path
):
    info = initialize(tmp_path / "case", "starter_decision")
    root = Path(info["root"])
    if mutation in (
        "seed_missing",
        "pointer_missing",
        "seed_rehashed",
        "foreign_reference",
    ):
        with sqlite3.connect(info["database"]) as conn:
            if mutation == "seed_missing":
                conn.execute("DELETE FROM seed_state")
            elif mutation == "pointer_missing":
                value = json.loads(
                    conn.execute("SELECT document FROM metadata").fetchone()[0]
                )
                value.update(creation="fixture", population=None)
                conn.execute("UPDATE metadata SET document=?", (canonical(value),))
            elif mutation == "foreign_reference":
                value = json.loads(
                    conn.execute("SELECT document FROM metadata").fetchone()[0]
                )
                value["population"]["case_id"] = "starter_payment"
                conn.execute("UPDATE metadata SET document=?", (canonical(value),))
            else:
                value = json.loads(
                    conn.execute("SELECT document FROM seed_state").fetchone()[0]
                )
                value["original"]["tables"]["business"][0]["document"] = "{}"
                value["original_sha256"] = digest(value["original"])
                value = state.sealed(
                    {k: v for k, v in value.items() if k != "content_sha256"}
                )
                conn.execute("UPDATE seed_state SET document=?", (canonical(value),))
    elif mutation == "control_rehashed":
        value = json.loads((root / "control.json").read_text())
        value["instance_id"] = "00000000-0000-0000-0000-000000000000"
        (root / "control.json").write_text(
            canonical(
                state.sealed({k: v for k, v in value.items() if k != "content_sha256"})
            )
        )
    elif mutation == "copied":
        shutil.copytree(root, tmp_path / "copy")
        root = tmp_path / "copy"
    elif mutation == "locked":
        (root / ".case-control.lock").write_text("owned by another attempt")
    else:
        (root / "fixture.json").rename(root / "original-fixture.json")
        (root / "fixture.json").symlink_to(root / "original-fixture.json")
    originals = {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}
    with pytest.raises((ValueError, OSError)):
        inspect_case(root)
    with pytest.raises((ValueError, OSError)):
        reset_case(root, tmp_path / "rejected-reset")
    assert {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()} == originals
    assert not (tmp_path / "rejected-reset").exists()


def test_case_collisions_and_nested_resets_preserve_original(tmp_path):
    info = initialize(tmp_path / "case", "starter_repeat")
    originals = {
        p.name: p.read_bytes() for p in Path(info["root"]).iterdir() if p.is_file()
    }
    for target in (info["root"], Path(info["root"]) / "child", tmp_path):
        with pytest.raises((OSError, ValueError)):
            reset_case(info["root"], target)
    with pytest.raises(FileExistsError):
        initialize(info["root"], "starter_decision")
    assert {
        p.name: p.read_bytes() for p in Path(info["root"]).iterdir() if p.is_file()
    } == originals


def test_current_source_does_not_trust_mutated_return_values_or_reuse_prior_valid_state(
    tmp_path,
):
    from renewal_agent.managed import ManagedWorkspace

    info = initialize(tmp_path / "case", "busy_subset")
    with ManagedWorkspace(info["database"]) as store:
        first = store.snapshot()
        actual = deepcopy(first)
        first["business"]["renewals"][0]["quantity"] = 999
        first["source"]["instance_id"] = "forged"
        first["original"]["tables"]["seed_state"].clear()
        assert store.snapshot() == actual
        with sqlite3.connect(info["database"]) as conn:
            value = deepcopy(actual["business"])
            value["renewals"][0]["payment_received"] = True
            conn.execute("UPDATE business SET document=?", (canonical(value),))
        with pytest.raises(ValueError, match="complete original decisions"):
            store.snapshot()
