"""Durable scoped cooperative tick; atomic local queue/CRM/task/decision source."""

from __future__ import annotations

import base64
import os
import secrets
import sqlite3
import stat
import uuid
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path

from . import managed_state as state
from .completion import completed
from .domain import customer_update, selected, tick_transition
from .source import canonical, day, digest, identifier, kind, parse, read_original


class ManagedWorkspace:
    def __init__(self, path, *, timeout=0.25):
        self._guard_fd = None
        self.close_attempted = self.closed = False
        supplied = Path(path).absolute()
        self.path = supplied.parent.resolve() / supplied.name
        self.timeout = timeout
        self.identity = self._identity()
        self.instance_id = self.source_raw_sha256 = None
        self.commit_guard = lambda: None
        try:
            self._guard_fd = os.open(
                self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            )
            current = self.snapshot()
            self.instance_id = current["source"]["instance_id"]
            self.source_raw_sha256 = state.sha256(
                current["original"]["source_document"].encode()
            )
        except BaseException as error:
            try:
                self.close()
            except BaseException as close_error:
                error.source_close_error = close_error
            error._managed_source = self
            raise

    def _guard_identity(self):
        if self.close_attempted or self._guard_fd is None:
            raise ValueError("managed source guard is closed")
        pinned = os.fstat(self._guard_fd)
        if (
            pinned.st_nlink != 1
            or f"{pinned.st_dev}:{pinned.st_ino}" != self.identity["database"]
            or self._identity() != self.identity
        ):
            raise ValueError("original managed database/root identity changed")

    def close(self):
        if self.close_attempted:
            raise ValueError("managed source guard close already attempted")
        self.close_attempted = True
        fd, self._guard_fd = self._guard_fd, None
        if fd is not None:
            os.close(fd)
        self.closed = True

    def __enter__(self):
        self._guard_identity()
        return self

    def __exit__(self, *_exception):
        self.close()

    def __del__(self):
        # Fallback for the declared manual source seam. Normal runtime must own
        # and confirm close explicitly; garbage collection is not run evidence.
        if getattr(self, "_guard_fd", None) is not None and not self.close_attempted:
            try:
                self.close()
            except OSError:
                pass

    def _identity(self):
        value = self.path.lstat()
        if (
            not stat.S_ISREG(value.st_mode)
            or value.st_nlink != 1
            or not 0 < value.st_size <= state.MAX_DATABASE_BYTES
        ):
            raise ValueError(
                "bounded regular unshared nonsymlink managed database required"
            )
        parent = self.path.parent.stat()
        return {
            "database": f"{value.st_dev}:{value.st_ino}",
            "parent": f"{parent.st_dev}:{parent.st_ino}",
            "name": self.path.name,
        }

    @classmethod
    def seed(cls, path, fixture):
        raw, _identity = read_original(fixture)
        data = parse(raw)
        supplied = Path(path).absolute()
        supplied.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        db = supplied.parent.resolve() / supplied.name
        fd = os.open(db, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created = os.fstat(fd)
        os.close(fd)
        parent = db.parent.stat()
        identity = {
            "database": f"{created.st_dev}:{created.st_ino}",
            "parent": f"{parent.st_dev}:{parent.st_ino}",
            "name": db.name,
        }
        doc = {
            "schema_version": state.SCHEMA_VERSION,
            "instance_id": str(uuid.uuid4()),
            "identity": identity,
            "fixture_base64": base64.b64encode(raw).decode(),
            "fixture_sha256": state.sha256(raw),
            "provenance": state.PROVENANCE,
            "creation": "fixture",
            "population": None,
        }
        conn = sqlite3.connect(db.as_uri() + "?mode=rw", uri=True, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA page_size=4096")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(f"PRAGMA application_id={state.APPLICATION_ID}")
            conn.execute(f"PRAGMA user_version={state.SCHEMA_VERSION}")
            for sql in state.TABLES.values():
                conn.execute(sql)
            conn.execute("INSERT INTO metadata VALUES (1,?)", (canonical(doc),))
            conn.execute("INSERT INTO business VALUES (1,?)", (canonical(data),))
            state.validate_snapshot(conn, identity, doc["instance_id"])
            current = db.lstat()
            if not stat.S_ISREG(current.st_mode) or (
                created.st_dev,
                created.st_ino,
            ) != (current.st_dev, current.st_ino):
                raise ValueError("managed seed destination changed")
            conn.commit()
            directory = os.open(db.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
        return cls(db)

    @contextmanager
    def _connect(self, *, write=False, control_only=False):
        self._guard_identity()
        conn = sqlite3.connect(
            self.path.as_uri() + "?mode=rw",
            uri=True,
            timeout=self.timeout,
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA trusted_schema=OFF")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            if control_only:
                state.schema(conn)
                doc, _fixture, original = state.header(
                    conn, self.identity, self.instance_id
                )
                before = {"source": doc, "original": {"source_document": original}}
            else:
                before = state.validate_snapshot(conn, self.identity, self.instance_id)
            if (
                self.source_raw_sha256 is not None
                and state.sha256(before["original"]["source_document"].encode())
                != self.source_raw_sha256
            ):
                raise ValueError("original managed source document changed")
            yield conn, before
            if write:
                if control_only:
                    state.schema(conn)
                    _doc, _fixture, original = state.header(
                        conn, self.identity, self.instance_id
                    )
                else:
                    after = state.validate_snapshot(
                        conn, self.identity, self.instance_id
                    )
                    original = after["original"]["source_document"]
                if original != before["original"]["source_document"]:
                    raise ValueError("managed source header changed inside operation")
                # Revocation must remain available after the controller's
                # business guard has observed cancellation or failed evidence.
                if not control_only:
                    self.commit_guard()
            self._guard_identity()
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def snapshot(self):
        with self._connect() as (_conn, current):
            return current

    def inspect_intent(self, today, renewal_ids=None):
        day(today)
        current = self.snapshot()
        ids = (
            renewal_ids
            if renewal_ids is not None
            else [r["id"] for r in current["business"]["renewals"]]
        )
        if (
            type(ids) is not list
            or not ids
            or any(type(i) is not str for i in ids)
            or len(set(ids)) != len(ids)
        ):
            raise ValueError("exact unique caller renewal IDs required")
        return state.intent(
            {
                "today": today,
                "targets": [deepcopy(selected(current["business"], i)) for i in ids],
                "source_context_sha256": current["context_sha256"],
            }
        )

    def _operation(self, conn, credential, *, allow_closed=False):
        state.hex_value(credential)
        rows = conn.execute(
            "SELECT * FROM operations WHERE token_sha256=?",
            (state.sha256(credential.encode()),),
        ).fetchall()
        if len(rows) != 1:
            raise ValueError("unknown private operation credential")
        row = dict(rows[0])
        op = state.operation(state.json_cell(row["document"]))
        if (
            op["id"] != row["id"]
            or op["token_sha256"] != row["token_sha256"]
            or op["instance_id"] != self.instance_id
        ):
            raise ValueError("original private operation identity differs")
        if not allow_closed and op["status"] != "active":
            raise ValueError("operation_closed")
        return op

    def register_operation(self, task_sha256, allow_writes, intent):
        state.hex_value(task_sha256)
        state.intent(intent)
        if type(allow_writes) is not bool:
            raise ValueError("explicit boolean write permission required")
        with self._connect(write=True) as (conn, current):
            if any(o["status"] == "active" for o in current["records"]["operations"]):
                raise ValueError("another cooperative model tick is active")
            if len(current["records"]["operations"]) >= state.LIMITS["operations"]:
                raise ValueError("managed operation budget exhausted")
            if intent["source_context_sha256"] != current["context_sha256"] or any(
                not state.same(selected(current["business"], r["id"]), r)
                for r in intent["targets"]
            ):
                raise ValueError("exact caller intent/current initial source required")
            credential = secrets.token_hex(32)
            op = state.sealed(
                {
                    "id": str(uuid.uuid4()),
                    "instance_id": self.instance_id,
                    "token_sha256": state.sha256(credential.encode()),
                    "task_sha256": task_sha256,
                    "allow_writes": allow_writes,
                    "intent": intent,
                    "initial_frontier": len(current["records"]["decisions"]),
                    "status": "active",
                }
            )
            conn.execute(
                "INSERT INTO operations VALUES (?,?,?)",
                (op["id"], op["token_sha256"], canonical(op)),
            )
            return {"credential": credential, "operation": op}

    def close_operation(self, credential):
        with self._connect(write=True, control_only=True) as (conn, _source):
            op = self._operation(conn, credential, allow_closed=True)
            closed = state.sealed(
                {
                    k: ("closed" if k == "status" else v)
                    for k, v in op.items()
                    if k != "content_sha256"
                }
            )
            if closed != op:
                conn.execute(
                    "UPDATE operations SET document=? WHERE id=?",
                    (canonical(closed), op["id"]),
                )
            return closed

    def current_operation(self, credential):
        with self._connect(control_only=True) as (conn, _source):
            return self._operation(conn, credential, allow_closed=True)

    def observe(self, credential, renewal_id, purpose):
        identifier(renewal_id)
        if type(purpose) is not str or purpose not in ("plan", "commit", "verify"):
            raise ValueError("exact observation purpose required")
        with self._connect(write=True) as (conn, current):
            op = self._operation(conn, credential)
            if not any(t["id"] == renewal_id for t in op["intent"]["targets"]):
                raise ValueError("renewal outside exact caller scope")
            if len(current["records"]["observations"]) >= state.LIMITS["observations"]:
                raise ValueError("genuine observation budget exhausted")
            token = secrets.token_hex(32)
            read = state.sealed(
                {
                    "id": str(uuid.uuid4()),
                    "sequence": len(current["records"]["observations"]) + 1,
                    "instance_id": self.instance_id,
                    "operation_id": op["id"],
                    "purpose": purpose,
                    "renewal_id": renewal_id,
                    "frontier": len(current["records"]["decisions"]),
                    "context_sha256": current["context_sha256"],
                    "value": deepcopy(selected(current["business"], renewal_id)),
                    "token_sha256": state.sha256(token.encode()),
                }
            )
            conn.execute(
                "INSERT INTO observations VALUES (?,?,?,?)",
                (read["id"], read["sequence"], op["id"], canonical(read)),
            )
            return {
                "value": read["value"],
                "observation": read,
                "proof": {"id": read["id"], "token": token},
            }

    @staticmethod
    def _read(current, op, proof, purpose, renewal_id=None, *, require_current=False):
        state.keys(proof, {"id", "token"})
        state.uuid_value(proof["id"])
        state.hex_value(proof["token"])
        found = next(
            (r for r in current["records"]["observations"] if r["id"] == proof["id"]),
            None,
        )
        if (
            found is None
            or found["operation_id"] != op["id"]
            or found["purpose"] != purpose
            or found["token_sha256"] != state.sha256(proof["token"].encode())
            or renewal_id is not None
            and found["renewal_id"] != renewal_id
            or require_current
            and found["context_sha256"] != current["context_sha256"]
        ):
            raise ValueError("genuine current own scoped observation required")
        return found

    def register_draft(self, credential, plan_proof, native_bytes):
        native, parsed = completed(native_bytes)
        with self._connect(write=True) as (conn, current):
            op = self._operation(conn, credential)
            plan = self._read(current, op, plan_proof, "plan")
            target = next(
                t for t in op["intent"]["targets"] if t["id"] == plan["renewal_id"]
            )
            if not state.same(target, plan["value"]):
                raise ValueError(
                    "native draft plan differs from original caller target"
                )
            if (
                len(current["records"]["drafts"]) >= state.LIMITS["drafts"]
                or sum(
                    d["operation_id"] == op["id"] for d in current["records"]["drafts"]
                )
                >= 32
            ):
                raise ValueError("completed draft budget exhausted")
            if any(
                d["operation_id"] == op["id"]
                and (d["native_id"] == native["id"] or d["plan_id"] == plan["id"])
                for d in current["records"]["drafts"]
            ):
                raise ValueError("native completion or plan was already recorded")
            item = state.sealed(
                {
                    "id": str(uuid.uuid4()),
                    "sequence": len(current["records"]["drafts"]) + 1,
                    "instance_id": self.instance_id,
                    "operation_id": op["id"],
                    "plan_id": plan["id"],
                    "native_base64": base64.b64encode(native_bytes).decode(),
                    "native_sha256": state.sha256(native_bytes),
                    "native_id": native["id"],
                    "reported_model": native["model"],
                    "value": parsed,
                    "source_sha256": digest(plan["value"]),
                }
            )
            conn.execute(
                "INSERT INTO drafts VALUES (?,?,?,?)",
                (item["id"], item["sequence"], op["id"], canonical(item)),
            )
            return item

    @staticmethod
    def _decision(
        conn,
        current,
        kind_name,
        args,
        after,
        result,
        *,
        op=None,
        read_id=None,
        plan_id=None,
        draft_id=None,
    ):
        if len(current["records"]["decisions"]) >= state.LIMITS["decisions"]:
            raise ValueError("atomic decision budget exhausted")
        sequence = len(current["records"]["decisions"]) + 1
        changed = not state.same(current["business"], after)
        record = state.sealed(
            {
                "id": str(uuid.uuid4()),
                "sequence": sequence,
                "instance_id": current["source"]["instance_id"],
                "operation_id": op["id"] if op else None,
                "kind": kind_name,
                "args": args,
                "read_id": read_id,
                "plan_id": plan_id,
                "draft_id": draft_id,
                "before_context_sha256": current["context_sha256"],
                "after_context_sha256": digest(
                    state.context(current["source"], sequence, after)
                ),
                "status": "applied" if changed else "unchanged",
                "result": result,
            }
        )
        if changed:
            conn.execute(
                "UPDATE business SET document=? WHERE id=1", (canonical(after),)
            )
        conn.execute(
            "INSERT INTO decisions VALUES (?,?,?,?)",
            (record["id"], sequence, record["operation_id"], canonical(record)),
        )
        return {"value": result, "decision": record}

    def change(self, renewal_id, expected_revision, updates):
        # Declared synthetic-human source seam, never a model tool.
        with self._connect(write=True) as (conn, current):
            after, result = customer_update(
                current["business"],
                renewal_id,
                updates,
                expected_revision=expected_revision,
            )
            return self._decision(
                conn,
                current,
                "customer_update",
                {
                    "renewal_id": renewal_id,
                    "expected_revision": expected_revision,
                    "updates": deepcopy(updates),
                },
                after,
                result,
            )

    def commit_tick(
        self, credential, renewal_id, plan_id, current_proof, draft_id=None
    ):
        identifier(renewal_id)
        state.uuid_value(plan_id)
        if draft_id is not None:
            state.uuid_value(draft_id)
        with self._connect(write=True) as (conn, current):
            op = self._operation(conn, credential)
            if not op["allow_writes"]:
                raise ValueError("writes_not_authorized")
            target = next(
                (t for t in op["intent"]["targets"] if t["id"] == renewal_id), None
            )
            if target is None:
                raise ValueError("renewal outside exact caller scope")
            read = self._read(
                current, op, current_proof, "commit", renewal_id, require_current=True
            )
            plan = next(
                (r for r in current["records"]["observations"] if r["id"] == plan_id),
                None,
            )
            if (
                plan is None
                or plan["operation_id"] != op["id"]
                or plan["purpose"] != "plan"
                or plan["renewal_id"] != renewal_id
                or plan["sequence"] >= read["sequence"]
            ):
                raise ValueError("genuine earlier own plan observation required")
            if any(
                d["operation_id"] == op["id"]
                and d["kind"] == "tick"
                and d["args"]["renewal_id"] == renewal_id
                for d in current["records"]["decisions"]
            ):
                raise ValueError("renewal already processed in this tick")
            payload = None
            if draft_id is not None:
                item = next(
                    (d for d in current["records"]["drafts"] if d["id"] == draft_id),
                    None,
                )
                if (
                    item is None
                    or item["operation_id"] != op["id"]
                    or item["plan_id"] != plan_id
                ):
                    raise ValueError("own completed original native draft required")
                payload = item["value"]
            args = {
                "renewal_id": renewal_id,
                "today": op["intent"]["today"],
                "planned_kind": kind(plan["value"], day(op["intent"]["today"])),
            }
            after, result = tick_transition(
                current["business"],
                renewal_id,
                day(args["today"]),
                args["planned_kind"],
                payload,
                draft_snapshot=target,
                identity_snapshot=target,
            )
            return self._decision(
                conn,
                current,
                "tick",
                args,
                after,
                result,
                op=op,
                read_id=read["id"],
                plan_id=plan_id,
                draft_id=draft_id,
            )
