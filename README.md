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

## Development audit active

The original six tests pass. Actual disposable probes reproduce stale in-window deadline and changed-recipient drafts, fabricated same-key outbox, coerced payment/revision, unsafe revision overflow, duplicate original JSON normalization and an aliased source write. The canonical LangGraph/default ChatOpenAI over owned loopback HTTP rejects finish_reason=length, but null/missing original finish_reason still queues a follow-up. These are synthetic offline reproductions, with no real delivery or paid provider. [PLAN.md](PLAN.md) records the current source/intent/native ownership/evidence/cases/distribution tasks and original private evidence. The existing normal tick remains unqualified while these tasks are implemented; enabled live model/budget/semantic+billing/Firedrill/real connectors/publication inputs stay parked.

### First source/draft checkpoint

36checks now cover original strict JSON/date/contact/boolean/revision/CRM/source validation, complete linked synthetic queue/task snapshots, original full draft-source binding, changed in-window deadline/recipient/account/revision, aliases, no-op exact bytes and revision exhaustion. Original six LangGraph/payment/cancel/overdue flows pass. Missing or altered source is rejected without repair; previous queue rows without source lineage require a fresh synthetic workspace. Manual source/save and graph injection hooks are trusted synthetic-human/test seams. Managed durable authority, explicit normal write permission and the owned original-native lifecycle remain active in PLAN.md; the normal model launch is still unqualified. Preserve old source/evidence when creating a fresh workspace.

### Managed source checkpoint

69checks now pass, including managed source/current intent/real processes. Model ticks will use an exclusive fresh SQLite source with original fixture/UUID/file/root/schema and complete transition replay. Queue, CRM, owner task and decision effects are atomic. One model scope is active at a time; synthetic-human customer changes can be reread while drafting. Each target needs its own fresh plan/current capabilities and separate write permission. This backend does not enable real email delivery.

```sh
uv sync --locked --group dev
uv run --locked renewal-state --db workspace/renewals.db init
uv run --locked renewal-state --db workspace/renewals.db inspect --today 2026-10-15 --renewal-id R-101
uv run --locked renewal-state --db workspace/renewals.db change R-101 --revision 1 --updates-json '{"payment_received":true}'
```

These commands are private, model-free source/intent inspection and an explicitly trusted synthetic-human update seam. Inspection grants no runtime read. The normal model tick is being connected to this managed backend and owned native completion/lifecycle controls; the earlier JSON model launch remains unqualified. Keep old source and evidence when creating a fresh database. [PLAN.md](PLAN.md) records the69check proof and remaining work.

Managed sources retain an owned file descriptor for their lifetime, preventing deleted-inode reuse from disguising a replacement. The private CLI closes it before success. The Linux gate exposed this gap in the first managed checkpoint; the original replacement regression remains and the product binding was corrected. Normal runtime must close the source guard explicitly alongside SDK/HTTP work.
