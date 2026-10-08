# Local-goal source reconciliation

## Source and scope

The pilot module, original tests, documentation and sanitized historical receipt
were recovered from the committed local checkout, not reconstructed from memory.
The original implementation is byte-for-byte preserved in this PR. Its code
origin is `672732c`, with refusal hardening in `a8f6a61` and documentation in
`98d1928`; the installed reconciliation checkout was `14f3b9d`.

The purpose is source parity: expose the previously local implementation as an
explicitly opt-in, reviewable module. It is not a new production driver rollout.
The computer-use skill now links to the canonical main-path documentation, which
will resolve after this PR is merged rather than depending on a pilot branch.

## Fresh verification

- All 23 original refusal/selection tests pass against current main's chooser.
- Two new integration tests exercise the real chooser and client response
  validator with offline transports. One owned in-memory fixture advances from
  bound text to a fresh observation and a click; its explicit postcondition is
  separate from selection. A malformed wire reply yields no executable action.
- The source was critically reviewed for outbound projection, exact binding,
  duplicate IDs, unsupported/ambiguous fields, sensitive values, consequential
  labels, stale results, confidence and safe abstention.
- The module only selects. Scope/freshness checks at execution time, step and
  deadline limits, secure-field preflight and actual-effect verification still
  belong to the executor. The conservative label filter is not approval.

The historical receipt is retained as historical synthetic warm-engine evidence.
It is **not** represented as a fresh live benchmark, native-app qualification,
independent review, or end-to-end performance result. No new paid calls, driver
launches, routing changes, gateway restart or default confidence changes were
performed. Existing installed pilot behavior is preserved.
