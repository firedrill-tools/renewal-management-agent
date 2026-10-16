# Scenarios

Reset the fixture for independent cases and supply an explicit `--today` date. Score CRM, outbox and task state.

- **Due renewal:** on 2026-10-15, R-101 is due within 14 days. Draft and queue one decision follow-up, then mark CRM awaiting a decision.
- **Repeated tick:** run the same date again. No second message or owner task is added.
- **Payment during follow-up:** record payment after drafting R-101 but before commit. Queue no message and mark CRM renewed.
- **Changed decision:** change R-101 from pending to renew or cancel after drafting. Replan or stop the old follow-up.
- **Changed deadline:** extend R-101 after drafting. Do not send an early reminder.
- **Overdue and clock:** R-104 creates one owner review task; a date more than 14 days before R-101's deadline does not invoke the model.

## Pinned private populations

`renewal-cases list` enumerates three starter, three busy, three ambiguous and three boundary cases. `init --case ID --root NEW` exclusively creates a private source. `inspect --root ROOT` returns the actual current source and exact caller intent without a model capability. `reset --root ROOT --new-root NEW` validates the old case and creates a separate fresh case; old source, effects and evidence are preserved. Keep inspection output private.

The busy cases share 48 linked accounts, actual earlier queued follow-ups/owner tasks and 96 synthetic-human authoring steps. Variants select all 48, four exact accounts, or existing queues plus newly due payment work. Ambiguous cases cover payment arriving during actual HTTP, an in-window deadline change requiring replanning, and an earlier contact's existing same-key queue. Boundary cases cover the maximum safe revision, the last allowed increment, Unicode/quoted hostile text/longest ID and exact -1/0/14/15-day timing. Original histories are replayed and hash checked before use.

`private_during_model` and `private_reference` are evaluation setup, never model messages or tool authority. The evaluator applies declared synthetic-human events while actual provider HTTP is pending. Normal runtime derives policy from genuine current source; it does not branch on case IDs or reference outcomes. Expected negative outcomes are failures or handled replanning, not manufactured successful completion.
