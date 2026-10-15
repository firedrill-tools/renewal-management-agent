# Scenarios

Reset the fixture for independent cases and supply an explicit `--today` date. Score CRM, outbox and task state.

- **Due renewal:** on 2026-10-15, R-101 is due within 14 days. Draft and queue one decision follow-up, then mark CRM awaiting a decision.
- **Repeated tick:** run the same date again. No second message or owner task is added.
- **Payment during follow-up:** record payment after drafting R-101 but before commit. Queue no message and mark CRM renewed.
- **Changed decision:** change R-101 from pending to renew or cancel after drafting. Replan or stop the old follow-up.
- **Changed deadline:** extend R-101 after drafting. Do not send an early reminder.
- **Overdue and clock:** R-104 creates one owner review task; a date more than 14 days before R-101's deadline does not invoke the model.
