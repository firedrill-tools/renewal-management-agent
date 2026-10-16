"""Private model-free managed source commands; no model or delivery authority."""

import argparse
import json
import sqlite3

from .managed import ManagedWorkspace
from .source import parse_json
from .assets import read_asset


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Private synthetic renewal source/intent commands"
    )
    parser.add_argument("--db", default="workspace/renewals.db")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument(
        "--fixture",
        help="Explicit original unshared fixture; default is pinned installed starter",
    )
    inspect = commands.add_parser("inspect")
    inspect.add_argument("--today")
    inspect.add_argument("--renewal-id", action="append")
    change = commands.add_parser(
        "change", help="Explicit trusted synthetic-human customer change"
    )
    change.add_argument("renewal_id")
    change.add_argument("--revision", required=True, type=int)
    change.add_argument("--updates-json", required=True)
    args = parser.parse_args(argv)
    try:
        store = (
            (
                ManagedWorkspace.seed(args.db, args.fixture)
                if args.fixture
                else ManagedWorkspace.seed_bytes(args.db, read_asset("starter.json"))
            )
            if args.command == "init"
            else ManagedWorkspace(args.db)
        )
        with store:
            if args.command == "init":
                value = {
                    "database": str(store.path),
                    "instance_id": store.instance_id,
                    "initialized": True,
                }
            elif args.command == "inspect":
                if args.renewal_id and not args.today:
                    raise ValueError(
                        "intent inspection requires an explicit application date"
                    )
                value = (
                    store.inspect_intent(args.today, args.renewal_id)
                    if args.today
                    else store.snapshot()
                )
            else:
                if len(args.updates_json) > 8192:
                    raise ValueError("bounded original customer change JSON required")
                value = store.change(
                    args.renewal_id,
                    args.revision,
                    parse_json(args.updates_json.encode("utf-8", errors="strict")),
                )
        print(json.dumps(value, indent=2, ensure_ascii=True, allow_nan=False))
    except (OSError, ValueError, sqlite3.Error) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=True))
        raise SystemExit(2) from error


if __name__ == "__main__":
    main()
