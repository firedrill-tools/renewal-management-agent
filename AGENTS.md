# Agent notes

Keep the scheduler tick explicit and cooperative. Reread payment, decision and deadline state before committing a follow-up; never treat a model draft as authorization to send. Preserve idempotent outbox keys and the overdue owner task. Keep data synthetic and sequential. Do not commit keys or runtime workspace state. Deterministic graph tests are offline evidence, not a live model or Firedrill proof.
