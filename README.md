# Renewal management agent

A LangGraph workflow runs one explicit scheduler tick over synthetic renewal, payment and CRM state. It classifies each renewal by deadline and current decision, asks an OpenAI model for a follow-up draft only when due, rereads the workspace, then queues a synthetic message or updates CRM. A payment, decision or deadline change between drafting and committing is rechecked before any follow-up is queued.

## Run locally

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```sh
uv sync --group dev
cp fixtures/starter.json workspace.json
```

Set `OPENAI_API_KEY` and an enabled `OPENAI_MODEL`, then run a tick for the explicit application date:

```sh
uv run renewal-tick --today 2026-10-15
```

Use `--renewal-id R-101` to process one renewal. The command runs and exits; it is not a background worker. An external scheduler must call it at the desired time. Advancing the `--today` value affects this cooperative application clock only. It does not alter a real payment system or keep a process alive.

The starter data includes a pending Northstar renewal due within 14 days, a cancellation, a paid renewal and an overdue undecided renewal. Follow-ups are queued in the local `outbox` with a key based on renewal, deadline and follow-up type, so repeating the same tick does not duplicate them. An overdue case creates a local owner task. CRM statuses are written to the same synthetic JSON workspace. The store is intended for sequential local execution, not concurrent production writes or real email delivery.

Reset with `cp fixtures/starter.json workspace.json`. `uv run --group dev pytest` exercises the compiled LangGraph, duplicate ticks, deadlines, a changed decision, an extended deadline and a payment arriving after draft generation. Tests inject a deterministic drafter to verify workflow behavior; they do not prove live OpenAI quality. Live provider and new Firedrill integration proofs remain pending. This private repository has no license pending portfolio publication decisions.

Explore related applications in the [Firedrill example catalog](https://github.com/firedrill-tools/firedrill-example-catalog). Product and setup references: [Firedrill](https://firedrill.run/) and the [official developer documentation](https://docs.firedrill.run/). Use documentation matching the qualified release; this repository records its current integration limits above.
