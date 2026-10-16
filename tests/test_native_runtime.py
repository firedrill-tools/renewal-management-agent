import json
from pathlib import Path

import pytest

from renewal_agent.runtime import run_tick
from renewal_agent.managed_state import digest
from renewal_agent.evidence import expand_source
from native_helper import environment, KEY


def rejected(options, pattern=None):
    with pytest.raises(BaseException, match=pattern) as caught:
        run_tick(**options)
    receipt = caught.value.evidence
    assert receipt["status"] == "failed"
    return receipt


def test_actual_async_langgraph_chatopenai_openai_original_http_and_managed_tick_complete(
    tmp_path,
):
    with environment(tmp_path) as e:
        result = run_tick(**e["options"])
        receipt = json.loads(Path(result["receipt_path"]).read_text())
        assert not e["errors"]
        assert len(e["requests"]) == 1
        assert (
            result["status"] == "completed" and result["business"]["workflow_complete"]
        )
        assert [r["action"] for r in result["results"]] == [
            "followup_queued",
            "crm_cancelled",
            "crm_renewed",
            "owner_review",
        ]
        assert len(result["business"]["applied_decision_ids"]) == 4
        assert (
            len(result["business"]["outbox"])
            == len(result["business"]["owner_tasks"])
            == 1
        )
        assert result["cleanup_confirmed"], receipt["lifecycle"]
        assert (
            result["usage"]["input"]["value"] == 10
            and result["usage"]["output"]["value"] == 2
        )
        assert (
            result["usage"]["cached_input"]["value"] == 0 and result["cost_usd"] is None
        )
        assert receipt["calls"][0]["response"]["body"]["unknown_original"]["preserved"]
        assert len(receipt["sdk"]) == 1 and len(receipt["nodes"]) == 24
        assert len(receipt["acknowledged_effects"]) == 4
        assert all(
            digest(
                expand_source(
                    receipt, {"sha256": sha, "context_sha256": v["context_sha256"]}
                )
            )
            == sha
            for sha, v in receipt["source_images"].items()
        )
        assert e["store"].snapshot()["records"]["operations"][0]["status"] == "closed"
        assert KEY not in json.dumps(receipt)
        wire = json.dumps(e["requests"])
        for private in (
            str(e["store"].path),
            e["store"].instance_id,
            receipt["operation"]["id"],
            receipt["operation"]["token_sha256"],
            "source_context_sha256",
            "fixture_base64",
        ):
            assert private not in wire
        assert (
            e["requests"][0]["organization"] is None
            and e["requests"][0]["project"] is None
        )


@pytest.mark.parametrize("fault", ["null", "missing", "length", "refusal"])
def test_unfinished_original_native_cannot_register_draft_or_queue_via_actual_normal_sdk(
    fault, tmp_path
):
    def mutate(body, n, request, handler, context):
        choice = body["choices"][0]
        if fault == "null":
            choice["finish_reason"] = None
        elif fault == "missing":
            choice.pop("finish_reason")
        elif fault == "length":
            choice["finish_reason"] = "length"
        else:
            choice["message"]["refusal"] = "Refused"
        return body

    with environment(tmp_path, mutate=mutate) as e:
        receipt = rejected(e["options"], "original.*(finish_reason|refused)")
        assert len(e["requests"]) == 1
        assert not e["store"].snapshot()["records"]["drafts"]
        assert not e["store"].snapshot()["records"]["decisions"]
        assert e["store"].snapshot()["business"] == e["store"].snapshot()["fixture"]
        assert receipt["cleanup_confirmed"], receipt["lifecycle"]
        assert receipt["reported_usage"]["input"]["value"] == 10


@pytest.mark.parametrize(
    "updates,action",
    [
        ({"payment_received": True}, "crm_renewed"),
        ({"deadline": "2026-10-28"}, "changed_before_send"),
        (
            {"email": "changed@cedar.example", "payment_received": True},
            "changed_before_send",
        ),
    ],
)
def test_actual_customer_commit_during_real_provider_http_is_reread_before_owned_graph_commit(
    updates, action, tmp_path
):
    def mutate(body, n, request, handler, context):
        context["store"].change("R-101", 1, updates)
        return body

    with environment(tmp_path, mutate=mutate, ids=["R-101"]) as e:
        result = run_tick(**e["options"])
        assert result["results"][0]["action"] == action
        assert not result["business"]["outbox"]
        assert result["cleanup_confirmed"]
        if action == "changed_before_send":
            assert (
                result["status"] == "needs_replan"
                and not result["business"]["workflow_complete"]
            )
            assert result["business"]["unchanged_decision_ids"]
        else:
            assert (
                result["status"] == "completed"
                and result["business"]["workflow_complete"]
            )
        decisions = e["store"].snapshot()["records"]["decisions"]
        assert decisions[0]["operation_id"] is None
        assert decisions[1]["operation_id"] is not None


def test_readonly_normal_actual_sdk_draft_does_not_update_crm_queue_or_tasks(tmp_path):
    with environment(tmp_path) as e:
        before = e["store"].snapshot()["business"]
        result = run_tick(**{**e["options"], "allow_writes": False})
        assert result["status"] == "read_only"
        assert len(e["requests"]) == 1
        assert e["store"].snapshot()["business"] == before
        assert not result["business"]["applied_decision_ids"]
        assert result["cleanup_confirmed"]


def test_missing_key_prepares_receipt_before_source_scope_clients_or_graph(tmp_path):
    with environment(tmp_path) as e:
        receipt = rejected({**e["options"], "api_key": ""}, "OPENAI_API_KEY")
        assert not any(receipt["allocation"].values())
        assert not e["requests"] and not receipt["calls"]
        assert not e["store"].snapshot()["records"]["operations"]
        assert receipt["cleanup_confirmed"], receipt["lifecycle"]


def test_normal_runtime_rejects_injected_drafter_or_sdk_client_before_allocation(
    tmp_path,
):
    with environment(tmp_path) as e:
        receipt = rejected({**e["options"], "drafter": lambda _: {}}, "injected SDK")
        assert not e["requests"] and not any(receipt["allocation"].values())
        assert receipt["cleanup_confirmed"]


def test_repeated_normal_tick_skips_redrafting_and_preserves_literal_business(tmp_path):
    with environment(tmp_path) as e:
        first = run_tick(**e["options"])
        before = e["store"].snapshot()["original"]["tables"]["business"][0]["document"]
        result = run_tick(
            **{
                **e["options"],
                "intent": e["store"].inspect_intent(e["options"]["today"]),
            }
        )
        assert len(e["requests"]) == 1
        assert not result["business"]["applied_decision_ids"]
        assert len(result["business"]["unchanged_decision_ids"]) == 4
        assert result["results"][0]["action"] == "already_queued"
        assert (
            result["usage"]["prepared_attempts"] == 0
            and result["usage"]["input"]["value"] is None
        )
        assert (
            e["store"].snapshot()["original"]["tables"]["business"][0]["document"]
            == before
        )
        assert result["cleanup_confirmed"] and first["cleanup_confirmed"]


@pytest.mark.parametrize("counters", ["missing", "invalid-and-zero"])
def test_original_missing_invalid_zero_usage_remains_distinct_from_sdk_normalization(
    counters, tmp_path
):
    def mutate(body, n, request, handler, context):
        if counters == "missing":
            body.pop("usage")
        else:
            body["usage"]["prompt_tokens"] = True
            body["usage"]["completion_tokens"] = 0
            body["usage"]["prompt_tokens_details"] = {}
        return body

    with environment(tmp_path, mutate=mutate) as e:
        result = run_tick(**e["options"])
        usage = result["usage"]
        assert usage["input"]["value"] is None
        if counters == "missing":
            assert usage["input"]["unknown"] == 1 and usage["output"]["unknown"] == 1
        else:
            assert (
                usage["input"]["invalid"] == 1 and usage["input"]["known_subtotal"] == 0
            )
            assert usage["output"]["value"] == 0 and usage["output"]["observed"] == 1
        assert usage["cached_input"]["unknown"] == 1
        assert result["cost_usd"] is None and result["cleanup_confirmed"]


def test_actual_normal_model_keeps_literal_file_mentions_without_expansion(tmp_path):
    from native_helper import FIXTURE

    canary = tmp_path / "literal.txt"
    canary.write_text("OWNED_CANARY_CONTENT_MUST_NOT_ENTER_THE_MODEL")
    data = json.loads(FIXTURE.read_text())
    data["renewals"][0]["account"] = "Northstar literal @" + str(canary)
    fixture = tmp_path / "literal-source.json"
    fixture.write_text(json.dumps(data))
    with environment(tmp_path, fixture=fixture, ids=["R-101"]) as e:
        result = run_tick(**e["options"])
        wire = json.dumps(e["requests"])
        assert "@" + str(canary) in wire
        assert canary.read_text() not in wire
        assert result["status"] == "completed" and result["cleanup_confirmed"]


def test_readonly_preview_drops_a_draft_made_inapplicable_by_payment_during_http(
    tmp_path,
):
    def mutate(body, n, request, handler, context):
        context["store"].change("R-101", 1, {"payment_received": True})
        return body

    with environment(tmp_path, mutate=mutate, ids=["R-101"]) as e:
        result = run_tick(**{**e["options"], "allow_writes": False})
        assert result["results"][0]["current_kind"] == "renewed"
        assert result["results"][0]["draft"] is None
        assert result["results"][0]["draft_current"] is False
        assert not result["business"]["applied_decision_ids"]
        assert not result["business"]["outbox"] and result["cleanup_confirmed"]
