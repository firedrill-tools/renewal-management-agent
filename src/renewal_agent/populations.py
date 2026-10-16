"""Pinned fictional histories; private references grant no runtime authority."""

from __future__ import annotations

import base64
import json
from copy import deepcopy
from functools import lru_cache
from pathlib import Path

from . import managed_state as state
from ._dataset_manifest import DATASET_BYTES, DATASET_CASES, DATASET_SHA256
from .domain import customer_update, selected, tick_transition
from .source import canonical, day, digest, keys, kind, parse, parse_json, read_original


def original_bytes(case, prefix="fixture"):
    raw = base64.b64decode(case[prefix + "_base64"], validate=True)
    if (
        base64.b64encode(raw).decode() != case[prefix + "_base64"]
        or state.sha256(raw) != case[prefix + "_sha256"]
    ):
        raise ValueError("original pinned renewal fixture bytes/hash differ")
    parse(raw)
    return raw


def replay(case):
    raw = original_bytes(case, "base_fixture")
    business = parse(raw)
    for n, step in enumerate(case["history"]):
        keys(
            step,
            {
                "sequence",
                "kind",
                "args",
                "read",
                "before_sha256",
                "after_sha256",
                "result",
            },
        )
        args = step["args"]
        observed = selected(business, args["renewal_id"])
        if (
            type(step["sequence"]) is not int
            or step["sequence"] != n + 1
            or step["before_sha256"] != state.sha256(raw)
            or not state.same(step["read"], observed)
        ):
            raise ValueError("original synthetic history has no exact consecutive read")
        if step["kind"] == "change":
            keys(args, {"renewal_id", "expected_revision", "updates"})
            after, result = customer_update(
                business,
                args["renewal_id"],
                args["updates"],
                expected_revision=args["expected_revision"],
            )
        elif step["kind"] == "tick":
            keys(args, {"renewal_id", "today", "planned_kind", "draft"})
            today = day(args["today"])
            if args["planned_kind"] != kind(observed, today):
                raise ValueError("synthetic history classification differs")
            after, result = tick_transition(
                business,
                args["renewal_id"],
                today,
                args["planned_kind"],
                args["draft"],
                draft_snapshot=observed,
            )
        else:
            raise ValueError("unknown original synthetic authoring step")
        if not state.same(business, after):
            raw = (
                json.dumps(after, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
            ).encode()
        if step["after_sha256"] != state.sha256(raw) or not state.same(
            step["result"], result
        ):
            raise ValueError(
                "original synthetic history effect differs from complete replay"
            )
        business = after
    if raw != original_bytes(case):
        raise ValueError(
            "original final fixture differs from complete authoring history"
        )
    day(case["today"])
    ids = case["renewal_ids"]
    if type(ids) is not list or not ids or len(ids) != len(set(ids)):
        raise ValueError("exact unique case targets required")
    for ident in ids:
        selected(business, ident)
    ref = case["reference"]
    for label, value in (
        ("prior_renewals", len(business["renewals"])),
        ("prior_outbox", len(business["outbox"])),
        ("prior_tasks", len(business["tasks"])),
        ("prior_steps", len(case["history"])),
    ):
        if type(ref[label]) is not int or ref[label] != value:
            raise ValueError("private case metrics contradict original source")
    return business


@lru_cache(maxsize=1)
def _dataset():
    raw, _identity = read_original(Path(__file__).parent / "data/populations.json")
    if len(raw) != DATASET_BYTES or state.sha256(raw) != DATASET_SHA256:
        raise ValueError("original pinned renewal population differs")
    value = parse_json(raw)
    if (
        value["id"] != "renewal-populations"
        or value["version"] != "1.0.0"
        or value["synthetic"] is not True
        or len(value["cases"]) != DATASET_CASES
    ):
        raise ValueError("original pinned renewal population identity differs")
    seen = set()
    for case in value["cases"]:
        if case["id"] in seen or case["profile"] not in (
            "starter",
            "busy",
            "ambiguous",
            "boundary",
        ):
            raise ValueError("duplicate or unknown original renewal case")
        seen.add(case["id"])
        replay(case)
    return value


def dataset():
    return deepcopy(_dataset())


def definition(case_id):
    if type(case_id) is not str:
        raise ValueError("exact pinned case identifier required")
    return deepcopy(_definition(case_id))


@lru_cache(maxsize=12)
def _definition(case_id):
    case = next((c for c in _dataset()["cases"] if c["id"] == case_id), None)
    if case is None:
        raise ValueError("unknown renewal case")
    return case


def reference(case):
    return {
        "id": "renewal-populations",
        "version": "1.0.0",
        "case_id": case["id"],
        "dataset_sha256": DATASET_SHA256,
        "definition_sha256": digest(case),
    }


def fixture_bytes(case):
    return original_bytes(case)


@lru_cache(maxsize=12)
def _baseline(case_id):
    case = _definition(case_id)
    business = parse(fixture_bytes(case))
    return reference(case), business, canonical(business)


def validate_reference(value):
    keys(value, {"id", "version", "case_id", "dataset_sha256", "definition_sha256"})
    if type(value["case_id"]) is not str:
        raise ValueError("exact pinned case identifier required")
    ref, _business, _raw = _baseline(value["case_id"])
    if not state.same(value, ref):
        raise ValueError("original pinned population reference differs")


def validate_seed(doc, tables, schema):
    case = _definition(doc["population"]["case_id"])
    ref, business, raw_business = _baseline(case["id"])
    if (
        not state.same(doc["population"], ref)
        or doc["fixture_sha256"] != case["fixture_sha256"]
        or doc["fixture_base64"] != case["fixture_base64"]
        or len(tables["seed_state"]) != 1
        or tables["seed_state"][0]["id"] != 1
    ):
        raise ValueError("original population reference/fixture/seed differs")
    seed = keys(
        state.json_cell(tables["seed_state"][0]["document"]),
        {
            "schema_version",
            "instance_id",
            "reference",
            "original",
            "original_sha256",
            "context_sha256",
            "provenance",
            "content_sha256",
        },
    )
    state.checked(seed)
    if (
        type(seed["schema_version"]) is not int
        or seed["schema_version"] != 1
        or seed["instance_id"] != doc["instance_id"]
        or not state.same(seed["reference"], ref)
        or not state.same(seed["provenance"], _dataset()["provenance"])
    ):
        raise ValueError("original population seed identity/provenance differs")
    original = keys(seed["original"], {"schema", "source_document", "tables"})
    if (
        digest(original) != seed["original_sha256"]
        or not state.same(original["schema"], schema)
        or original["source_document"]
        != canonical({**doc, "creation": "fixture", "population": None})
    ):
        raise ValueError("original population initialization source/hash differs")
    prior = keys(original["tables"], set(tables))
    if any(prior[t] for t in state.LIMITS):
        raise ValueError("population seed cannot import runtime authority")
    if not state.same(prior["business"], [{"id": 1, "document": raw_business}]) or seed[
        "context_sha256"
    ] != digest(state.context(doc, 0, business)):
        raise ValueError("original population baseline business/context differs")
    return seed
