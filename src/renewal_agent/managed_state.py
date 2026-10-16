"""Exact original SQLite source, genuine reads and complete renewal replay."""

from __future__ import annotations

import base64
import hashlib
import uuid
from copy import deepcopy

from .completion import completed
from .domain import customer_update, selected, tick_transition
from .source import (
    MAX_BYTES,
    canonical,
    day,
    digest,
    identifier,
    keys,
    kind,
    parse,
    parse_json,
    renewal,
    validate,
)

APPLICATION_ID = 0x4644524E
SCHEMA_VERSION = 1
MAX_DATABASE_BYTES = 64 * 1024 * 1024
LIMITS = {
    "operations": 128,
    "observations": 1024,
    "drafts": 128,
    "decisions": 512,
    "seed_state": 1,
}
TABLES = {
    "metadata": "CREATE TABLE metadata (id INTEGER PRIMARY KEY CHECK(id=1), document TEXT NOT NULL) STRICT",
    "business": "CREATE TABLE business (id INTEGER PRIMARY KEY CHECK(id=1), document TEXT NOT NULL) STRICT",
    "operations": "CREATE TABLE operations (id TEXT PRIMARY KEY, token_sha256 TEXT NOT NULL UNIQUE, document TEXT NOT NULL) STRICT",
    "observations": "CREATE TABLE observations (id TEXT PRIMARY KEY, sequence INTEGER NOT NULL UNIQUE, operation_id TEXT, document TEXT NOT NULL) STRICT",
    "drafts": "CREATE TABLE drafts (id TEXT PRIMARY KEY, sequence INTEGER NOT NULL UNIQUE, operation_id TEXT NOT NULL, document TEXT NOT NULL) STRICT",
    "decisions": "CREATE TABLE decisions (id TEXT PRIMARY KEY, sequence INTEGER NOT NULL UNIQUE, operation_id TEXT, document TEXT NOT NULL) STRICT",
    "seed_state": "CREATE TABLE seed_state (id INTEGER PRIMARY KEY CHECK(id=1), document TEXT NOT NULL) STRICT",
}
PROVENANCE = {
    "kind": "explicit_synthetic_fixture_author",
    "prior_model_evidence": None,
    "prior_human_authentication": None,
    "real_delivery": None,
}


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def same(a, b):
    return canonical(a) == canonical(b)


def uuid_value(value):
    if type(value) is not str:
        raise ValueError("canonical managed UUID required")
    try:
        valid = str(uuid.UUID(value)) == value
    except (ValueError, AttributeError):
        valid = False
    if not valid:
        raise ValueError("canonical managed UUID required")
    return value


def hex_value(value):
    if (
        type(value) is not str
        or len(value) != 64
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise ValueError("original SHA256 or opaque token required")
    return value


def sealed(value):
    return {**deepcopy(value), "content_sha256": digest(value)}


def checked(value):
    hex_value(value["content_sha256"])
    if (
        digest({k: v for k, v in value.items() if k != "content_sha256"})
        != value["content_sha256"]
    ):
        raise ValueError("original managed record hash differs")
    return value


def json_cell(raw):
    if (
        type(raw) is not str
        or len(raw.encode("utf-8", errors="strict")) > 8 * 1024 * 1024
    ):
        raise ValueError("bounded complete original managed JSON cell required")
    return parse_json(raw.encode())


def intent(value):
    keys(value, {"today", "targets", "source_context_sha256"})
    day(value["today"])
    hex_value(value["source_context_sha256"])
    if type(value["targets"]) is not list or not 1 <= len(value["targets"]) <= 128:
        raise ValueError("bounded exact caller renewal targets required")
    ids = [renewal(r)["id"] for r in value["targets"]]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate caller renewal target")
    return value


def operation(value):
    keys(
        value,
        {
            "id",
            "instance_id",
            "token_sha256",
            "task_sha256",
            "allow_writes",
            "intent",
            "initial_frontier",
            "status",
            "content_sha256",
        },
    )
    uuid_value(value["id"])
    uuid_value(value["instance_id"])
    hex_value(value["token_sha256"])
    hex_value(value["task_sha256"])
    intent(value["intent"])
    if (
        type(value["allow_writes"]) is not bool
        or type(value["initial_frontier"]) is not int
        or not 0 <= value["initial_frontier"] <= LIMITS["decisions"]
        or type(value["status"]) is not str
        or value["status"] not in ("active", "closed")
    ):
        raise ValueError("exact managed operation permission/frontier/status required")
    return checked(value)


def rows(conn, table, order, limit):
    result = [
        dict(r)
        for r in conn.execute(f"SELECT * FROM {table} ORDER BY {order}").fetchmany(
            limit + 1
        )
    ]
    if len(result) > limit:
        raise ValueError("complete managed table exceeds bounded capacity")
    return result


def schema(conn):
    if conn.execute("PRAGMA page_size").fetchone()[0] != 4096:
        raise ValueError("original managed page size differs")
    if conn.execute("PRAGMA page_count").fetchone()[0] * 4096 > MAX_DATABASE_BYTES:
        raise ValueError("projected managed database exceeds size bound before commit")
    if (
        conn.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID
        or conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION
        or conn.execute("PRAGMA journal_mode").fetchone()[0].lower() != "delete"
        or conn.execute("PRAGMA encoding").fetchone()[0] != "UTF-8"
    ):
        raise ValueError("original managed renewal storage header required")
    actual = [
        dict(r)
        for r in conn.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name"
        )
    ]
    expected = [
        {"type": "table", "name": name, "tbl_name": name, "sql": sql}
        for name, sql in TABLES.items()
    ]
    for table in ("operations", "observations", "drafts", "decisions"):
        for n in (1, 2):
            expected.append(
                {
                    "type": "index",
                    "name": f"sqlite_autoindex_{table}_{n}",
                    "tbl_name": table,
                    "sql": None,
                }
            )
    if actual != sorted(expected, key=lambda r: (r["type"], r["name"])):
        raise ValueError(
            "original managed schema differs; unknown tables/indexes/triggers/views unsupported"
        )
    if [tuple(r) for r in conn.execute("PRAGMA quick_check")] != [("ok",)]:
        raise ValueError("original managed database integrity failed")
    return actual


def header(conn, identity, instance_id=None):
    entries = rows(conn, "metadata", "id", 1)
    if len(entries) != 1 or entries[0]["id"] != 1:
        raise ValueError("one original managed renewal source required")
    original = entries[0]["document"]
    doc = keys(
        json_cell(original),
        {
            "schema_version",
            "instance_id",
            "identity",
            "fixture_base64",
            "fixture_sha256",
            "provenance",
            "creation",
            "population",
        },
    )
    if (
        type(doc["schema_version"]) is not int
        or doc["schema_version"] != SCHEMA_VERSION
        or not same(doc["identity"], identity)
        or not same(doc["provenance"], PROVENANCE)
    ):
        raise ValueError("original managed renewal source/identity/provenance differs")
    uuid_value(doc["instance_id"])
    if instance_id is not None and instance_id != doc["instance_id"]:
        raise ValueError("original managed renewal UUID changed")
    if doc["creation"] != "fixture" or doc["population"] is not None:
        raise ValueError(
            "unsupported managed population source; pinned case qualification required"
        )
    try:
        raw = base64.b64decode(doc["fixture_base64"], validate=True)
    except (ValueError, TypeError):
        raise ValueError("original renewal fixture bytes required") from None
    if (
        len(raw) > MAX_BYTES
        or base64.b64encode(raw).decode() != doc["fixture_base64"]
        or sha256(raw) != hex_value(doc["fixture_sha256"])
    ):
        raise ValueError("original managed fixture bytes/hash differ")
    return doc, parse(raw), original


def context(doc, frontier, business):
    return {
        "source_sha256": digest(
            {k: v for k, v in doc.items() if k not in {"creation", "population"}}
        ),
        "frontier": frontier,
        "business_sha256": digest(business),
    }


def typed_records(tables, doc):
    records = {}
    for table in ("operations", "observations", "drafts", "decisions"):
        values = []
        for n, row in enumerate(tables[table]):
            value = json_cell(row["document"])
            if (
                type(value) is not dict
                or value.get("id") != row["id"]
                or value.get("instance_id") != doc["instance_id"]
            ):
                raise ValueError("original managed record identity differs")
            uuid_value(value["id"])
            checked(value)
            if table == "operations":
                operation(value)
                if value["token_sha256"] != row["token_sha256"]:
                    raise ValueError("original operation credential hash differs")
            elif (
                type(value.get("sequence")) is not int
                or value["sequence"] != n + 1
                or row["sequence"] != n + 1
                or value.get("operation_id") != row["operation_id"]
            ):
                raise ValueError(
                    "complete consecutive managed record sequence required"
                )
            values.append(value)
        records[table] = values
    return records


def validate_snapshot(conn, identity, instance_id=None):
    original_schema = schema(conn)
    doc, fixture, original_header = header(conn, identity, instance_id)
    tables = {"business": rows(conn, "business", "id", 1)}
    tables.update(
        {
            t: rows(
                conn,
                t,
                "rowid" if t in ("operations", "seed_state") else "sequence",
                limit,
            )
            for t, limit in LIMITS.items()
        }
    )
    if (
        len(tables["business"]) != 1
        or tables["business"][0]["id"] != 1
        or tables["seed_state"]
    ):
        raise ValueError(
            "one current business source and no unqualified seed authority required"
        )
    current = validate(json_cell(tables["business"][0]["document"]))
    records = typed_records(tables, doc)
    ops = {o["id"]: o for o in records["operations"]}
    reads = {r["id"]: r for r in records["observations"]}
    drafts = {d["id"]: d for d in records["drafts"]}
    if sum(o["status"] == "active" for o in ops.values()) > 1:
        raise ValueError("only one cooperative model tick may be active")
    for read in reads.values():
        keys(
            read,
            {
                "id",
                "sequence",
                "instance_id",
                "operation_id",
                "purpose",
                "renewal_id",
                "frontier",
                "context_sha256",
                "value",
                "token_sha256",
                "content_sha256",
            },
        )
        if (
            type(read["purpose"]) is not str
            or read["purpose"] not in ("plan", "commit", "verify")
            or type(read["frontier"]) is not int
            or not 0 <= read["frontier"] <= len(records["decisions"])
            or read["operation_id"] not in ops
        ):
            raise ValueError("original current scoped renewal observation required")
        identifier(read["renewal_id"])
        hex_value(read["token_sha256"])
        hex_value(read["context_sha256"])
        renewal(read["value"])
    draft_completions = {}
    native_ids = set()
    for item in drafts.values():
        keys(
            item,
            {
                "id",
                "sequence",
                "instance_id",
                "operation_id",
                "plan_id",
                "native_base64",
                "native_sha256",
                "native_id",
                "reported_model",
                "value",
                "source_sha256",
                "content_sha256",
            },
        )
        uuid_value(item["plan_id"])
        plan = reads.get(item["plan_id"])
        if (
            item["operation_id"] not in ops
            or plan is None
            or plan["operation_id"] != item["operation_id"]
            or plan["purpose"] != "plan"
        ):
            raise ValueError(
                "original native draft has no own current plan observation"
            )
        try:
            raw = base64.b64decode(item["native_base64"], validate=True)
        except (ValueError, TypeError):
            raise ValueError("original native draft bytes required") from None
        if (
            sha256(raw) != hex_value(item["native_sha256"])
            or base64.b64encode(raw).decode() != item["native_base64"]
        ):
            raise ValueError("original native draft bytes/hash differ")
        native, value = completed(raw)
        native_key = (item["operation_id"], native["id"])
        if (
            native_key in native_ids
            or item["native_id"] != native["id"]
            or item["reported_model"] != native["model"]
            or not same(item["value"], value)
            or item["source_sha256"] != digest(plan["value"])
        ):
            raise ValueError(
                "original native draft/source authority differs or is replayed"
            )
        native_ids.add(native_key)
        original_target = next(
            (
                r
                for r in ops[item["operation_id"]]["intent"]["targets"]
                if r["id"] == plan["renewal_id"]
            ),
            None,
        )
        if not same(original_target, plan["value"]) or kind(
            plan["value"], day(ops[item["operation_id"]]["intent"]["today"])
        ) not in ("decision_followup", "payment_followup"):
            raise ValueError("original native draft differs from exact caller target")
        draft_completions[item["id"]] = value
    business = deepcopy(fixture)
    row_states = [{r["id"]: r for r in business["renewals"]}]
    contexts = [context(doc, 0, business)]
    consumed, used_plans, used_drafts, processed_targets = set(), set(), set(), set()
    for n, decision in enumerate(records["decisions"]):
        keys(
            decision,
            {
                "id",
                "sequence",
                "instance_id",
                "operation_id",
                "kind",
                "args",
                "read_id",
                "plan_id",
                "draft_id",
                "before_context_sha256",
                "after_context_sha256",
                "status",
                "result",
                "content_sha256",
            },
        )
        if decision["before_context_sha256"] != digest(contexts[-1]):
            raise ValueError("original decision has a different before context")
        args = decision["args"]
        if decision["kind"] == "customer_update":
            if decision["operation_id"] is not None or any(
                decision[k] is not None for k in ("read_id", "plan_id", "draft_id")
            ):
                raise ValueError(
                    "customer changes are a declared synthetic-human seam only"
                )
            keys(args, {"renewal_id", "expected_revision", "updates"})
            after, result = customer_update(
                business,
                args["renewal_id"],
                args["updates"],
                expected_revision=args["expected_revision"],
            )
        elif decision["kind"] == "tick":
            uuid_value(decision["plan_id"])
            uuid_value(decision["read_id"])
            if decision["draft_id"] is not None:
                uuid_value(decision["draft_id"])
            op = ops.get(decision["operation_id"])
            if op is None:
                raise ValueError(
                    "normal tick requires exact current operation authority"
                )
            keys(args, {"renewal_id", "today", "planned_kind"})
            read = reads.get(decision["read_id"])
            plan = reads.get(decision["plan_id"])
            original_target = next(
                (r for r in op["intent"]["targets"] if r["id"] == args["renewal_id"]),
                None,
            )
            target_key = (op["id"], args["renewal_id"])
            if (
                not op["allow_writes"]
                or original_target is None
                or args["today"] != op["intent"]["today"]
                or read is None
                or plan is None
                or read["operation_id"] != op["id"]
                or plan["operation_id"] != op["id"]
                or read["purpose"] != "commit"
                or plan["purpose"] != "plan"
                or read["renewal_id"] != args["renewal_id"]
                or plan["renewal_id"] != args["renewal_id"]
                or read["frontier"] != n
                or read["context_sha256"] != digest(contexts[-1])
                or plan["sequence"] >= read["sequence"]
                or plan["frontier"] > n
                or args["planned_kind"] != kind(plan["value"], day(args["today"]))
                or read["id"] in consumed
                or plan["id"] in used_plans
                or target_key in processed_targets
            ):
                raise ValueError(
                    "current own unused scoped plan/read/exact tick intent required"
                )
            value = None
            if decision["draft_id"] is not None:
                item = drafts.get(decision["draft_id"])
                if (
                    item is None
                    or item["operation_id"] != op["id"]
                    or item["plan_id"] != plan["id"]
                    or item["id"] in used_drafts
                ):
                    raise ValueError("current own completed native draft required")
                value = draft_completions[item["id"]]
                used_drafts.add(item["id"])
            after, result = tick_transition(
                business,
                args["renewal_id"],
                day(args["today"]),
                args["planned_kind"],
                value,
                draft_snapshot=original_target,
                identity_snapshot=original_target,
            )
            consumed.add(read["id"])
            used_plans.add(plan["id"])
            processed_targets.add(target_key)
        else:
            raise ValueError("unsupported original renewal decision kind")
        expected_status = "unchanged" if same(business, after) else "applied"
        after_context = context(doc, n + 1, after)
        if (
            decision["status"] != expected_status
            or not same(result, decision["result"])
            or decision["after_context_sha256"] != digest(after_context)
        ):
            raise ValueError(
                "original renewal decision/effect contradicts complete transition replay"
            )
        business = after
        # Retain historical immutable row views, not every growing outbox/task
        # document. Complete business effects still replay one step at a time.
        prior_rows = row_states[-1]
        row_states.append(
            {
                r["id"]: prior_rows[r["id"]] if same(prior_rows[r["id"]], r) else r
                for r in business["renewals"]
            }
        )
        contexts.append(after_context)
    if not same(business, current):
        raise ValueError(
            "current renewal business differs from complete original decisions"
        )
    for op in ops.values():
        initial = op["initial_frontier"]
        if (
            initial >= len(row_states)
            or op["intent"]["source_context_sha256"] != digest(contexts[initial])
            or any(
                not same(row_states[initial].get(t["id"]), t)
                for t in op["intent"]["targets"]
            )
        ):
            raise ValueError(
                "original caller intent differs from complete initial source"
            )
    for read in reads.values():
        op = ops[read["operation_id"]]
        if (
            read["frontier"] < op["initial_frontier"]
            or read["context_sha256"] != digest(contexts[read["frontier"]])
            or not same(
                read["value"], row_states[read["frontier"]].get(read["renewal_id"])
            )
            or not any(t["id"] == read["renewal_id"] for t in op["intent"]["targets"])
        ):
            raise ValueError("original scoped observation was never current source")
    original = {
        "schema": original_schema,
        "source_document": original_header,
        "tables": tables,
    }
    return {
        "source": doc,
        "fixture": fixture,
        "original": original,
        "sha256": digest(original),
        "business": business,
        "context": contexts[-1],
        "context_sha256": digest(contexts[-1]),
        "records": records,
    }
