# Whole pre-call I/O deadline qualification

The first preflight repair bounded discovery and selection only. A source-bound native host fixture reproduced settings, logging, suppression reads and feedback writes waiting beyond the native host's two-second limit. This is a controlled failure-mode reproduction, not proof those dependencies caused historical production stalls.

## Repair

The same 1.5-second deadline now covers settings and read-only selection work. Only a timely foreground result can update routing or return a suggestion. Optional logging and feedback writes share the remaining budget, so a timely suggestion survives slow bookkeeping. Such bookkeeping can finish later, but pending-token publication checks the actual turn-record identity, including reused turn IDs. A skill loaded before its delayed feedback write commits is settled as accepted. Admission remains six outstanding callbacks without a queue. Explicit tools, thresholds, privacy and routing modes are unchanged. Python cannot cancel already-started I/O or guarantee an OS scheduling deadline.

## Exercised results

- Full canonical suite: 1,721 tests, OK. Seven whole-hook tests cover normal suggestions, each held I/O boundary, reused-turn isolation and load-before-disk-commit settlement.
- Actual installed native hook dispatcher: all six cases return without a native skipped hook; the five held cases return within 1.527 to 1.576 seconds in this observation. Settings, selection and suppression timeouts emit no suggestion or later feedback. Slow optional logging/writes preserve the timely suggestion.
- Omission control: the exact previous committed callback fails both unchanged settings/logging timing assertions in an owned disposable source fixture. Restoring current source passes both. An initial incomplete fixture omitted the existing `_wire` helper and failed import; that run is retained separately and is not counted as a negative-control success.
- Fresh installed PluginManager with actual configured service and public text: successful selection and suggestion in 0.2694 seconds. Held selection returns in 1.5147 seconds, with no late turn mutation. The host emits an existing external-skill-root warning because published procedures are linked into a disposable home; it does not prevent the suggestion.
- Canonical root-only installer exercised after prior managed bytes matched the previous commit and were backed up. All 164 compared installed files match source; 48 config/state hashes remain unchanged. No other-profile links, official host source, model, scheduled job or gateway service were changed.

Receipts: `evidence/preflight-budget/whole-hook-before.json`, `whole-hook-after.json`, `whole-hook-negative.json`, `whole-hook-summary.json` and `whole-hook-installed-host.json`.

Resources reused: native PluginManager/dispatch for the host boundary; the canonical preflight admission helper for bounded work; existing feedback persistence and tests for real outcome settlement; root-only canonical installer for deployment provenance. The production runtime remains unaccepted until normal gateway adoption and real elapsed-time/owner-path checks are observed. Installation and fresh-process inference are not proof an in-flight gateway reloaded, six concurrent agents met responsiveness targets, or a full 48-hour no-stall window passed.
