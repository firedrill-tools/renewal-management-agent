# Renewal management agent

A LangGraph application runs one explicit renewal tick over fictional local renewal, payment and CRM data. An OpenAI model drafts a follow-up only when it is due. The application rereads current source, checks caller permission and identity, then atomically queues a synthetic message, updates CRM or creates an overdue owner task. Payment, cancellation, deadline and contact changes during drafting are handled from current state.

## Run

Requires Python3.12+ and [uv](https://docs.astral.sh/uv/).

```sh
uv sync --locked --group dev
uv run --locked renewal-state --db workspace/renewals.db init
uv run --locked renewal-state --db workspace/renewals.db inspect --today 2026-10-15 --renewal-id R-101
```

Set `OPENAI_API_KEY` and an enabled `OPENAI_MODEL`. No model is selected by the example. Run a read-only preview:

```sh
uv run --locked renewal-tick --db workspace/renewals.db --today 2026-10-15 --renewal-id R-101
```

To authorize the exact current renewal/date/contact intent, save the private model-free inspection and pass it separately from write permission:

```sh
uv run --locked renewal-state --db workspace/renewals.db inspect --today 2026-10-15 --renewal-id R-101 > workspace/tick-intent.json
uv run --locked renewal-tick --db workspace/renewals.db --today 2026-10-15 --renewal-id R-101 --allow-writes --intent-json "$(cat workspace/tick-intent.json)"
```

Inspect again if current source changed before registration. `--output DIR` selects private receipts; `--timeout-seconds` sets a shared0.01..300second limit. `--model` and `--base-url` configure an explicit enabled ChatCompletions model and exact HTTPS `/v1` endpoint or an owned loopback HTTP provider. A real provider request can incur cost. Tests use only owned local HTTP and synthetic keys.

The application runs and exits. An external scheduler must invoke the next tick. `--today` is the cooperative application date; it does not advance OS time, a payment service or a background worker. Model-free synthetic-human changes use the private CLI:

```sh
uv run --locked renewal-state --db workspace/renewals.db change R-101 --revision 1 --updates-json '{"payment_received":true}'
```

Use the actual current revision. This is a declared trusted source-authoring seam, with no real payment or CRM connector.

## Operation contract

A fresh managed SQLite source retains original fixture bytes, UUID/root/file identity, exact schema, complete decision replay, original observations and completed draft bytes. A retained source descriptor prevents deleted-inode reuse from disguising replacement while the source owner is active. Source and descriptor ownership are explicit; missing, copied, replaced, shared, aliased, corrupt, inconsistent or modified authority is rejected without repair. Initialization is exclusive; keep old source and receipts when creating a new source.

Only one model tick is active per source. Exact caller date/full target intent plus a separate write gate and genuine own plan/current reads precede effects. Customer updates can commit while a draft is pending. Same-identity payment/cancellation updates current CRM independently; changed decision/deadline requires replanning or deferral. Changed renewal/account/email identity blocks queue, CRM and task effects and needs fresh caller intent. An existing follow-up key prevents duplication. An unchanged repeat skips redrafting and preserves literal business JSON. Queue, CRM, owner task and scoped decision commit in one SQLite transaction; private receipt publication and initialization are separate effects.

The normal path uses the actual async compiled LangGraph, ChatOpenAI and owned sync/async OpenAI clients. Nonstream original ChatCompletions JSON is checked before SDK parsing: explicit stop, one complete assistant JSON draft, no refusal/error/tool call and strict bounded subject/body. Original content and parsed fields must agree with the actual SDK result before draft registration. Unexpected synchronous dispatch is denied. Model, origin, key, strict schema, no retries,32requests,4096output tokens, byte/phase/shared bounds and scoped trace disabling are explicit. Generic/injected SDK/drafter/client replacements are unsupported by the normal runtime. `run_tick_async` is available from `renewal_agent.runtime`; `run_tick` is its synchronous wrapper.

Private `artifacts/<UUID>/trace.json` receipts retain source schema/header/raw cells through verified content-addressed rows, original HTTP request/decoded response bytes and unknown fields, SDK normalization, node/source observations, actual scoped decisions, earlier acknowledged effects and cleanup. `renewal_agent.evidence.expand_source(data, reference)` reconstructs and verifies each complete original SQL image. Private controls/credentials/source paths/seed material stay out of model messages; literal file mentions remain literal. Reported counters distinguish unknown/invalid/observed-zero from SDK normalization. Actual billing and real delivery are unknown.

Cancellation, shared deadline and normal SIGINT/SIGTERM/SIGHUP durably close scope before draining graph/native work, then close SDK/HTTP pools and the source guard once. Failed closure/recording/current source cannot return success. Earlier committed effects and current valid or unknown state remain explicit. Readonly previews discard drafts made inapplicable by current payment/decision/deadline/contact changes and identify their observation context. A handled identity/deadline change returns `status: needs_replan` with `workflow_complete: false`; the remaining selected targets continue. These are cooperative controls, not an OS sandbox or remote billing guarantee.

## Development and proof limits

```sh
uv run --locked --group dev pytest -q
```

116checks pass locally:71source/managed/actual process checks plus45normal actual SDK/HTTP/compiled graph/CLI/lifecycle checks. They include ordinary queue/CRM/owner-task flow, current customer changes during real HTTP, no-op/readonly, original incomplete/malformed/replayed/oversized/partial/non2xx responses, usage provenance, literal canary, request budget, missing key, partial construction, actual cancellation/deadline/parent/all CLI signals and failed native/source/receipt closes after committed effects. [PLAN.md](PLAN.md) records exact evidence and baseline reproductions. The explicit `run_synthetic_tick`/`build_graph` drafter hooks and JSON Workspace are separate trusted synthetic-human/source-test seams; no unowned default model launch remains.

Twelve coherent cases/private preserving reset and fresh complete/production-only distribution qualification remain active. Enabled live model/budget/semantic and billing review, qualified new Firedrill persisted cases, real connectors/accounts and publication/license stay parked. This private repository has no license pending publication decisions. Offline controlled outputs do not prove live model decision quality or real customer delivery.

Explore the [Firedrill example catalog](https://github.com/firedrill-tools/firedrill-example-catalog). Product references: [Firedrill](https://firedrill.run/) and its [official documentation](https://docs.firedrill.run/); use the documentation matching a qualified release.
