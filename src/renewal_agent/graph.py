"""LangGraph workflow: load, classify, draft, recheck, commit, report."""

from __future__ import annotations

from copy import deepcopy
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ConfigDict, Field

from .workspace import Workspace, kind
from .source import day, draft as validate_draft


class EmailDraft(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    subject: str = Field(min_length=5, max_length=240)
    body: str = Field(min_length=15, max_length=8192)


class State(TypedDict, total=False):
    renewal_id: str
    today: str
    snapshot: dict
    planned_kind: str
    draft: dict | None
    draft_snapshot: dict
    result: dict


def build_graph(workspace: Workspace, drafter, after_draft=None):
    def load(state: State):
        return {"snapshot": workspace.get(state["renewal_id"])}

    def classify(state: State):
        return {"planned_kind": kind(state["snapshot"], day(state["today"]))}

    def draft(state: State):
        if state["planned_kind"] not in ("decision_followup", "payment_followup"):
            return {"draft": None}
        original = deepcopy(state["snapshot"])
        value = drafter(state)
        parsed = (
            value.model_dump()
            if isinstance(value, EmailDraft)
            else EmailDraft.model_validate(value).model_dump()
        )
        validate_draft(parsed)
        return {"draft": parsed, "draft_snapshot": original}

    def recheck(state: State):
        if after_draft:
            after_draft(state)
        return {"snapshot": workspace.get(state["renewal_id"])}

    def commit(state: State):
        return {
            "result": workspace.commit_tick(
                state["renewal_id"],
                day(state["today"]),
                state["planned_kind"],
                state["draft"],
                draft_snapshot=state.get("draft_snapshot"),
            )
        }

    graph = StateGraph(State)
    for name, function in (
        ("load", load),
        ("classify", classify),
        ("draft", draft),
        ("recheck", recheck),
        ("commit", commit),
    ):
        graph.add_node(name, function)
    graph.add_edge(START, "load")
    graph.add_edge("load", "classify")
    graph.add_edge("classify", "draft")
    graph.add_edge("draft", "recheck")
    graph.add_edge("recheck", "commit")
    graph.add_edge("commit", END)
    return graph.compile()


def run_synthetic_tick(
    workspace: Workspace,
    today: str,
    renewal_id: str | None = None,
    drafter=None,
    after_draft=None,
) -> list[dict]:
    day(today)
    if drafter is None:
        raise ValueError("explicit trusted synthetic-human drafter required")
    ids = (
        [renewal_id]
        if renewal_id
        else [item["id"] for item in workspace.read()["renewals"]]
    )
    graph = build_graph(workspace, drafter, after_draft)
    return [
        graph.invoke({"renewal_id": item, "today": today})["result"] for item in ids
    ]


def run_tick(*args, **options):
    """Normal managed runtime; source-test drafter hooks are a separate seam."""
    from .runtime import run_tick as owned_tick

    return owned_tick(*args, **options)
