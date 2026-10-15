"""Synthetic renewal/CRM state with an explicit cooperative clock."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path


def kind(renewal: dict, today: date) -> str:
    if renewal["decision"] == "cancel":
        return "cancelled"
    if renewal["payment_received"]:
        return "renewed"
    days = (date.fromisoformat(renewal["deadline"]) - today).days
    if days < 0:
        return "overdue"
    if days > 14:
        return "not_due"
    return "payment_followup" if renewal["decision"] == "renew" else "decision_followup"


class Workspace:
    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        self.read()

    def read(self) -> dict:
        data = json.loads(self.path.read_text())
        if not all(isinstance(data.get(key), list) for key in ("renewals", "outbox", "tasks")):
            raise ValueError("invalid renewal workspace")
        return data

    def save(self, data: dict) -> None:
        temp = self.path.with_name(self.path.name + ".tmp")
        temp.write_text(json.dumps(data, indent=2) + "\n")
        temp.replace(self.path)

    def get(self, renewal_id: str) -> dict:
        found = next((item for item in self.read()["renewals"] if item["id"] == renewal_id), None)
        if found is None:
            raise ValueError("unknown renewal")
        return found

    def change(self, renewal_id: str, *, decision: str | None = None,
               payment_received: bool | None = None, deadline: str | None = None) -> dict:
        data = self.read()
        row = next((item for item in data["renewals"] if item["id"] == renewal_id), None)
        if row is None:
            raise ValueError("unknown renewal")
        if decision is not None:
            if decision not in ("pending", "renew", "cancel"):
                raise ValueError("invalid decision")
            row["decision"] = decision
        if payment_received is not None:
            row["payment_received"] = payment_received
        if deadline is not None:
            date.fromisoformat(deadline)
            row["deadline"] = deadline
        row["revision"] += 1
        self.save(data)
        return row

    def commit_tick(self, renewal_id: str, today: date, planned_kind: str,
                    draft: dict | None) -> dict:
        data = self.read()
        row = next((item for item in data["renewals"] if item["id"] == renewal_id), None)
        if row is None:
            raise ValueError("unknown renewal")
        current_kind = kind(row, today)
        if current_kind == "cancelled":
            row["crm_status"] = "cancelled"
            action = "crm_cancelled"
        elif current_kind == "renewed":
            row["crm_status"] = "renewed"
            action = "crm_renewed"
        elif current_kind == "overdue":
            key = f"{renewal_id}:{row['deadline']}:overdue"
            if not any(item["key"] == key for item in data["tasks"]):
                data["tasks"].append({"key": key, "renewal_id": renewal_id,
                                      "reason": "deadline passed without decision or payment"})
            row["crm_status"] = "owner_review"
            action = "owner_review"
        elif current_kind == "not_due":
            action = "not_due"
        elif current_kind != planned_kind:
            action = "changed_before_send"
        else:
            if not draft or len(draft.get("subject", "")) < 5 or len(draft.get("body", "")) < 15:
                raise ValueError("invalid follow-up draft")
            key = f"{renewal_id}:{row['deadline']}:{current_kind}"
            if not any(item["key"] == key for item in data["outbox"]):
                data["outbox"].append({"key": key, "to": row["email"],
                                       "subject": draft["subject"], "body": draft["body"]})
                action = "followup_queued"
            else:
                action = "already_queued"
            row["crm_status"] = "awaiting_payment" if current_kind == "payment_followup" else "awaiting_decision"
        if action != "not_due":
            self.save(data)
        return {"renewal_id": renewal_id, "action": action, "current_kind": current_kind,
                "crm_status": row["crm_status"], "revision": row["revision"]}
