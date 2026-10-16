"""Complete bounded original synthetic renewal JSON and linked queue records."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from datetime import date
from pathlib import Path

MAX_INTEGER = 2**53 - 1
MAX_BYTES = 4 * 1024 * 1024


def canonical(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8", errors="strict")).hexdigest()


def keys(value, fields):
    if type(value) is not dict or set(value) != set(fields):
        raise ValueError("original renewal fields differ from declared schema")
    return value


def text(value, minimum, maximum):
    if (
        type(value) is not str
        or not minimum <= len(value) <= maximum
        or "\x00" in value
    ):
        raise ValueError("bounded original business text required")
    value.encode("utf-8", errors="strict")
    return value


def identifier(value):
    if type(value) is not str or not re.fullmatch(r"[A-Z][A-Z0-9_-]{0,63}", value):
        raise ValueError("canonical renewal identifier required")
    return value


def email(value):
    if (
        type(value) is not str
        or len(value) > 254
        or not re.fullmatch(
            r"[a-z0-9][a-z0-9._+-]{0,63}@[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*\.example",
            value,
        )
    ):
        raise ValueError("canonical fictional .example email required")
    return value


def day(value):
    if type(value) is not str or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("canonical YYYY-MM-DD application date required")
    result = date.fromisoformat(value)
    if result.isoformat() != value:
        raise ValueError("canonical application date required")
    return result


def revision(value):
    if type(value) is not int or not 1 <= value <= MAX_INTEGER:
        raise ValueError("exact positive safe renewal revision required")
    return value


def next_revision(value):
    if revision(value) == MAX_INTEGER:
        raise ValueError("revision_exhausted")
    return value + 1


def renewal(row):
    keys(
        row,
        {
            "id",
            "account",
            "email",
            "deadline",
            "decision",
            "payment_received",
            "crm_status",
            "revision",
        },
    )
    identifier(row["id"])
    text(row["account"], 1, 200)
    email(row["email"])
    day(row["deadline"])
    revision(row["revision"])
    if (
        type(row["decision"]) is not str
        or row["decision"] not in ("pending", "renew", "cancel")
        or type(row["payment_received"]) is not bool
        or type(row["crm_status"]) is not str
        or row["crm_status"]
        not in (
            "open",
            "cancelled",
            "renewed",
            "owner_review",
            "awaiting_payment",
            "awaiting_decision",
        )
    ):
        raise ValueError("exact renewal decision/payment/CRM status required")
    status = row["crm_status"]
    if (
        status == "cancelled"
        and row["decision"] != "cancel"
        or status == "renewed"
        and (not row["payment_received"] or row["decision"] == "cancel")
        or status == "awaiting_payment"
        and (row["decision"] != "renew" or row["payment_received"])
        or status == "awaiting_decision"
        and (row["decision"] != "pending" or row["payment_received"])
        or status == "owner_review"
        and (row["decision"] == "cancel" or row["payment_received"])
    ):
        raise ValueError("CRM status contradicts current renewal decision/payment")
    return row


def kind(row, today):
    renewal(row)
    if type(today) is not date:
        raise ValueError("explicit application date required")
    if row["decision"] == "cancel":
        return "cancelled"
    if row["payment_received"]:
        return "renewed"
    days = (day(row["deadline"]) - today).days
    if days < 0:
        return "overdue"
    if days > 14:
        return "not_due"
    return "payment_followup" if row["decision"] == "renew" else "decision_followup"


def draft(value):
    keys(value, {"subject", "body"})
    text(value["subject"], 5, 240)
    text(value["body"], 15, 8192)
    if any(ord(c) < 32 or ord(c) == 127 for c in value["subject"]):
        raise ValueError("subject must be a single original text line")
    return value


def validate(data):
    keys(data, {"renewals", "outbox", "tasks"})
    for name, limit in (("renewals", 128), ("outbox", 1024), ("tasks", 512)):
        if type(data[name]) is not list or len(data[name]) > limit:
            raise ValueError("complete bounded renewal tables required")
    if not data["renewals"]:
        raise ValueError("nonempty synthetic renewal source required")
    ids = [renewal(row)["id"] for row in data["renewals"]]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate renewal identity")
    seen = set()
    for item in data["outbox"]:
        keys(
            item,
            {
                "key",
                "renewal_id",
                "to",
                "subject",
                "body",
                "source",
                "today",
                "kind",
                "source_sha256",
                "draft_sha256",
                "delivery",
            },
        )
        source = renewal(item["source"])
        current_kind = kind(source, day(item["today"]))
        draft_value = draft({k: item[k] for k in ("subject", "body")})
        expected_key = f"{source['id']}:{source['deadline']}:{current_kind}"
        if (
            source["id"] not in ids
            or item["renewal_id"] != source["id"]
            or current_kind not in ("decision_followup", "payment_followup")
            or item["kind"] != current_kind
            or item["key"] != expected_key
            or item["key"] in seen
            or item["to"] != source["email"]
            or item["source_sha256"] != digest(source)
            or item["draft_sha256"] != digest(draft_value)
            or item["delivery"] != "synthetic_queued"
        ):
            raise ValueError("original queued follow-up source/key/draft links differ")
        seen.add(item["key"])
    seen = set()
    for item in data["tasks"]:
        keys(item, {"key", "renewal_id", "reason", "source", "today", "source_sha256"})
        source = renewal(item["source"])
        text(item["reason"], 1, 500)
        if (
            source["id"] not in ids
            or item["renewal_id"] != source["id"]
            or kind(source, day(item["today"])) != "overdue"
            or item["key"] != f"{source['id']}:{source['deadline']}:overdue"
            or item["key"] in seen
            or item["source_sha256"] != digest(source)
        ):
            raise ValueError("original overdue task source/key links differ")
        seen.add(item["key"])
    canonical(data).encode("utf-8", errors="strict")
    return data


def parse(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate original renewal JSON key")
            result[key] = value
        return result

    def constant(value):
        raise ValueError("nonfinite original renewal JSON")

    return validate(
        json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=pairs,
            parse_constant=constant,
        )
    )


def read_original(path):
    path = Path(path)
    before = path.lstat()
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or not 0 < before.st_size <= MAX_BYTES
    ):
        raise ValueError("bounded regular unshared nonsymlink renewal source required")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(fd)
        with os.fdopen(os.dup(fd), "rb") as source:
            raw = source.read(MAX_BYTES + 1)
        after, current = os.fstat(fd), path.lstat()
        if (
            (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
            or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
            or not stat.S_ISREG(current.st_mode)
            or after.st_size != opened.st_size
            or after.st_mtime_ns != opened.st_mtime_ns
            or len(raw) != opened.st_size
            or len(raw) > MAX_BYTES
        ):
            raise ValueError("original renewal source changed while reading")
        return raw, (opened.st_dev, opened.st_ino)
    finally:
        os.close(fd)
