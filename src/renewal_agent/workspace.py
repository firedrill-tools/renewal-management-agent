"""Sequential synthetic renewal source and exact current draft bindings."""

from __future__ import annotations

import json
import os
import uuid
from copy import deepcopy
from datetime import date
from pathlib import Path

from .source import (
    MAX_BYTES,
    canonical,
    day,
    digest,
    draft as validate_draft,
    identifier,
    kind,
    next_revision,
    parse,
    read_original,
    renewal,
    validate,
)


class Workspace:
    def __init__(self, path: str | Path):
        supplied = Path(path).absolute()
        self.path = supplied.parent.resolve() / supplied.name
        self.identity = self.original = None
        self.parent_identity = (
            self.path.parent.stat().st_dev,
            self.path.parent.stat().st_ino,
        )
        self.read()

    def read(self) -> dict:
        raw, identity = read_original(self.path)
        if (
            self.identity is not None
            and identity != self.identity
            or (self.path.parent.stat().st_dev, self.path.parent.stat().st_ino)
            != self.parent_identity
        ):
            raise ValueError("original renewal file/root identity changed")
        data = parse(raw)
        self.identity, self.original = identity, raw
        return data

    def save(self, data: dict) -> bool:
        # Declared trusted synthetic-human seam; normal managed authority remains
        # task1. Sequential source guard is not a distributed transaction claim.
        validate(data)
        raw = (
            json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
        ).encode()
        if len(raw) > MAX_BYTES:
            raise ValueError("complete renewal source exceeds4MiB")
        current, identity = read_original(self.path)
        if current != self.original or identity != self.identity:
            raise ValueError("original renewal source changed before write")
        if canonical(parse(current)) == canonical(data):
            return False
        temporary = self.path.parent / (".renewal-" + str(uuid.uuid4()) + ".tmp")
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as output:
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
            fresh, identity = read_original(self.path)
            if fresh != current or identity != self.identity:
                raise ValueError("original renewal source changed during staging")
            os.replace(temporary, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            self.identity = self.original = None
            self.read()
            return True
        finally:
            temporary.unlink(missing_ok=True)

    def get(self, renewal_id: str) -> dict:
        identifier(renewal_id)
        found = next(
            (r for r in self.read()["renewals"] if r["id"] == renewal_id), None
        )
        if found is None:
            raise ValueError("unknown renewal")
        return found

    def change(
        self, renewal_id: str, *, decision=None, payment_received=None, deadline=None
    ) -> dict:
        identifier(renewal_id)
        if decision is not None and (
            type(decision) is not str or decision not in ("pending", "renew", "cancel")
        ):
            raise ValueError("invalid decision")
        if payment_received is not None and type(payment_received) is not bool:
            raise ValueError("exact boolean payment required")
        if deadline is not None:
            day(deadline)
        data = self.read()
        row = next((r for r in data["renewals"] if r["id"] == renewal_id), None)
        if row is None:
            raise ValueError("unknown renewal")
        before = deepcopy(row)
        for key, value in (
            ("decision", decision),
            ("payment_received", payment_received),
            ("deadline", deadline),
        ):
            if value is not None:
                row[key] = value
        if canonical(before) != canonical(row):
            row["revision"] = next_revision(before["revision"])
            row["crm_status"] = "open"
            self.save(data)
        return row

    def commit_tick(
        self,
        renewal_id: str,
        today: date,
        planned_kind: str,
        draft: dict | None,
        *,
        draft_snapshot=None,
    ) -> dict:
        identifier(renewal_id)
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
        data = self.read()
        row = next((r for r in data["renewals"] if r["id"] == renewal_id), None)
        if row is None:
            raise ValueError("unknown renewal")
        before = deepcopy(row)
        current_kind = kind(row, today)
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
            validate_draft(draft)
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
        if action not in ("not_due", "changed_before_send"):
            self.save(data)
        return {
            "renewal_id": renewal_id,
            "action": action,
            "current_kind": current_kind,
            "crm_status": row["crm_status"],
            "revision": row["revision"],
        }
