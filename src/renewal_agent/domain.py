"""Pure complete renewal transitions shared by human source and managed ticks."""

from copy import deepcopy
from datetime import date

from .source import (
    canonical,
    day,
    digest,
    draft as validate_draft,
    identifier,
    kind,
    next_revision,
    renewal,
    revision,
    validate,
)


def selected(data, renewal_id):
    identifier(renewal_id)
    row = next((r for r in data["renewals"] if r["id"] == renewal_id), None)
    if row is None:
        raise ValueError("unknown renewal")
    return row


def customer_update(original, renewal_id, updates, *, expected_revision=None):
    data = deepcopy(validate(original))
    row = selected(data, renewal_id)
    if (
        type(updates) is not dict
        or not updates
        or not set(updates)
        <= {"account", "email", "decision", "payment_received", "deadline"}
    ):
        raise ValueError("exact customer update fields required")
    if expected_revision is not None and revision(expected_revision) != row["revision"]:
        raise ValueError("stale renewal revision")
    before = deepcopy(row)
    candidate = {**row, **updates, "crm_status": "open"}
    renewal(candidate)
    if any(canonical(row[key]) != canonical(value) for key, value in updates.items()):
        row.update(candidate)
        row["revision"] = next_revision(before["revision"])
    validate(data)
    return data, deepcopy(row)


def tick_transition(
    original,
    renewal_id,
    today,
    planned_kind,
    draft=None,
    *,
    draft_snapshot=None,
    identity_snapshot=None,
):
    data = deepcopy(validate(original))
    row = selected(data, renewal_id)
    if (
        type(today) is not date
        or type(planned_kind) is not str
        or planned_kind
        not in (
            "cancelled",
            "renewed",
            "overdue",
            "not_due",
            "decision_followup",
            "payment_followup",
        )
    ):
        raise ValueError("exact cooperative date/planned kind required")
    before = deepcopy(row)
    current_kind = kind(row, today)
    if identity_snapshot is not None:
        renewal(identity_snapshot)
        if identity_snapshot["id"] != renewal_id:
            raise ValueError("tick caller identity belongs to another renewal")
        if any(
            identity_snapshot[field] != before[field] for field in ("account", "email")
        ):
            return data, {
                "renewal_id": renewal_id,
                "action": "changed_before_send",
                "current_kind": current_kind,
                "crm_status": row["crm_status"],
                "revision": row["revision"],
            }
    if current_kind == "cancelled":
        row["crm_status"], action = "cancelled", "crm_cancelled"
    elif current_kind == "renewed":
        row["crm_status"], action = "renewed", "crm_renewed"
    elif current_kind == "overdue":
        key = f"{renewal_id}:{row['deadline']}:overdue"
        if not any(t["key"] == key for t in data["tasks"]):
            data["tasks"].append(
                {
                    "key": key,
                    "renewal_id": renewal_id,
                    "reason": "deadline passed without recorded payment or completed cancellation",
                    "source": before,
                    "today": today.isoformat(),
                    "source_sha256": digest(before),
                }
            )
        row["crm_status"], action = "owner_review", "owner_review"
    elif current_kind == "not_due":
        action = "not_due"
    elif current_kind != planned_kind:
        action = "changed_before_send"
    elif draft_snapshot is None:
        raise ValueError("original complete draft source required")
    elif canonical(renewal(draft_snapshot)) != canonical(before):
        action = "changed_before_send"
    else:
        key = f"{renewal_id}:{row['deadline']}:{current_kind}"
        existing = next((m for m in data["outbox"] if m["key"] == key), None)
        if existing is not None:
            if (
                existing["to"] != row["email"]
                or existing["source"]["account"] != row["account"]
            ):
                raise ValueError(
                    "existing follow-up has a different original recipient/account"
                )
            action = "already_queued"
        else:
            validate_draft(draft)
            data["outbox"].append(
                {
                    "key": key,
                    "renewal_id": renewal_id,
                    "to": row["email"],
                    **deepcopy(draft),
                    "source": before,
                    "today": today.isoformat(),
                    "kind": current_kind,
                    "source_sha256": digest(before),
                    "draft_sha256": digest(draft),
                    "delivery": "synthetic_queued",
                }
            )
            action = "followup_queued"
        row["crm_status"] = (
            "awaiting_payment"
            if current_kind == "payment_followup"
            else "awaiting_decision"
        )
    if canonical(before) != canonical(row):
        row["revision"] = next_revision(before["revision"])
    validate(data)
    return data, {
        "renewal_id": renewal_id,
        "action": action,
        "current_kind": current_kind,
        "crm_status": row["crm_status"],
        "revision": row["revision"],
    }
