import argparse
import json

from .graph import run_tick
from .workspace import Workspace


def main():
    parser = argparse.ArgumentParser(description="Run one cooperative renewal scheduler tick")
    parser.add_argument("--state", default="workspace.json")
    parser.add_argument("--today", required=True, help="Explicit YYYY-MM-DD application date")
    parser.add_argument("--renewal-id")
    args = parser.parse_args()
    try:
        workspace = Workspace(args.state)
        print(json.dumps({"today": args.today,
                          "results": run_tick(workspace, args.today, args.renewal_id)}, indent=2))
    except (ValueError, RuntimeError) as error:
        parser.exit(1, f"{error}\n")
