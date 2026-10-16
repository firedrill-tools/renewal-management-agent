"""Normal owned model tick CLI; scheduling remains one explicit invocation."""

import argparse
import asyncio
import json
import signal

from .runtime import Cancellation, run_tick_async
from .source import parse_json


async def main_async(argv=None):
    parser = argparse.ArgumentParser(
        description="Run one owned synthetic renewal scheduler tick"
    )
    parser.add_argument("--db", default="workspace/renewals.db")
    parser.add_argument("--today", required=True)
    parser.add_argument("--renewal-id", action="append")
    parser.add_argument("--model")
    parser.add_argument("--base-url")
    parser.add_argument("--allow-writes", action="store_true")
    parser.add_argument("--intent-json")
    parser.add_argument("--output", default="artifacts")
    parser.add_argument("--timeout-seconds", type=float, default=120)
    args = parser.parse_args(argv)
    cancellation = Cancellation()
    loop = asyncio.get_running_loop()
    installed = []
    for name in ("SIGINT", "SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is not None:
            loop.add_signal_handler(
                sig, cancellation.cancel, name + " cancelled renewal tick"
            )
            installed.append(sig)
    try:
        if args.intent_json is not None and len(args.intent_json) > 256 * 1024:
            raise ValueError("caller intent JSON exceeds complete bound")
        intent = (
            parse_json(args.intent_json.encode("utf-8", errors="strict"))
            if args.intent_json is not None
            else None
        )
        result = await run_tick_async(
            args.db,
            args.today,
            args.renewal_id,
            args.model,
            args.allow_writes,
            intent=intent,
            base_url=args.base_url,
            timeout_seconds=args.timeout_seconds,
            output_root=args.output,
            cancellation=cancellation,
        )
        print(json.dumps(result, indent=2, ensure_ascii=True, allow_nan=False))
        return 0
    except BaseException as error:
        evidence = getattr(error, "evidence", None)
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": str(error) or type(error).__name__,
                    "receipt_path": getattr(error, "receipt_path", None),
                    "output_dir": getattr(error, "output_dir", None),
                    "business": evidence.get("observed_business") if evidence else None,
                    "acknowledged_effects": (
                        evidence.get("acknowledged_effects") if evidence else None
                    ),
                    "usage": evidence.get("reported_usage") if evidence else None,
                    "cleanup_confirmed": (
                        evidence.get("cleanup_confirmed") if evidence else None
                    ),
                },
                indent=2,
                ensure_ascii=True,
                allow_nan=False,
            )
        )
        return 1
    finally:
        for sig in installed:
            loop.remove_signal_handler(sig)


def main():
    raise SystemExit(asyncio.run(main_async()))


if __name__ == "__main__":
    main()
