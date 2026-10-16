"""Sequential synthetic renewal source and exact current draft bindings."""

from __future__ import annotations

import json
import os
import uuid
from datetime import date
from pathlib import Path

from .source import (
    MAX_BYTES,
    canonical,
    identifier,
    kind,
    parse,
    read_original,
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
        from .domain import customer_update

        updates = {
            k: v
            for k, v in (
                ("decision", decision),
                ("payment_received", payment_received),
                ("deadline", deadline),
            )
            if v is not None
        }
        data = self.read()
        if not updates:
            return self.get(renewal_id)
        changed, row = customer_update(data, renewal_id, updates)
        if canonical(changed) != canonical(data):
            self.save(changed)
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
        from .domain import tick_transition

        data = self.read()
        changed, result = tick_transition(
            data, renewal_id, today, planned_kind, draft, draft_snapshot=draft_snapshot
        )
        if canonical(changed) != canonical(data):
            self.save(changed)
        return result
