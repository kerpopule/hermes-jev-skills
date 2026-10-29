# Jev ecosystem patterns: adoption and limits

Source: [GitTrend's Jev roundup](https://x.com/gittrend0x/status/2115073744989221250).
This change adopts applicable patterns, not third-party runtimes or their
performance claims. No upstream code was copied and no new runtime dependency
was added. Playwright is optional for the Chromium smoke test only.

## Source-to-implementation receipt

- **Jev Browser**, [source snapshot](https://github.com/jkudish/jev-browser/tree/817b09eea6e21aad836b5ed32a613c68e5644a08), MIT.
  Reuse: executor-owned budgets, separate proposal/execution observations and
  honest terminal outcomes. Our opt-in runner now executes predict/act explicitly,
  records action receipts even when a following observation fails, and observes
  checkbox/select/expanded/scroll state. It checks an allowed host before input,
  stops after three executed actions without progress and returns completed,
  blocked or degraded. A model's DONE is not independent verification. This is
  not a general-purpose network sandbox; page scripts can make network requests.
- **Jev Search**, [source snapshot](https://github.com/superagents-lab/jev-search/tree/1bfe835e8b4320444a5304043243d4e96ed278b7), MIT.
  Reuse: decide sources and freshness before retrieval; distinguish failed
  sources from genuine empty results; merge duplicate URLs with provenance.
  `jev search-plan` selects only caller-authored complete query/source/time plans.
  `jev search-merge` groups locally; `jev search` with `source_batches` screens
  every offered variant before producing a deduplicated reading list. A flagged,
  clipped or unread duplicate withholds its companions, including overflow beyond
  the existing result cap. Selected fetch URLs retain their exact original bytes;
  grouping preserves SPA fragments and semantic query bytes/order. A single
  offered plan needs no model. No Search1API integration, engine activation or
  paid entitlement is installed.
- **Codex Router**, [source snapshot](https://github.com/0xNatoshi/jev-codex-router/tree/8701ef788aa8cb0948f299538747fb01029d32b8), MIT, archived.
  Model-plus-effort selection already exists in our plugin. Do not stack a second
  classifier or replace supported middleware with an archived router. Retain and
  exercise existing manual-pin, provider/request-binding, effort-shape and
  concurrent-session regressions in `tests/test_plugin_middleware.py`. The routing
  skill now explicitly requires total-cost accounting and labels historical
  simulated savings as counterfactual, not production results. No live mode,
  model pool or default was changed.
- **QuantDinger**, [source snapshot](https://github.com/OpenByteInc/QuantDinger/tree/8b2af21c288d87a5571d67ee6b2bfa860b06a4ba).
  Backend Apache-2.0; separate frontend/mobile/brand terms apply. Reuse: typed
  decisions, an auditable outcome and code-owned execution policy. Reject its
  trading-specific fail-open policy as a general approval pattern. Strengthen
  our existing strict tiebreak so explicit error, drift or fallback metadata can
  never turn an approve label into permission. A deny remains a deny. Advisory
  hook decisions still return to the host's ordinary approval flow. No trading,
  exchange connection, human-approval bypass or gate-mode promotion is installed.
- **Doom in Claude Code**, [source snapshot](https://github.com/abelardodiaz/doom-in-claude-code/tree/33d967ee48195a608652d7ca3e8a8a9a1a9f2e13), MIT.
  Reuse: reserve a decision model for uncertain higher-level choices, not every
  deterministic control tick. Browser progress/budgets, merging and single-plan
  handling are local code. Reinforce this boundary in browser and model-routing
  skills; existing Blender code-first fixtures remain part of the regression
  suite. Do not install Doom or make game/latency claims from these fixtures.

The repository's existing `search`, privacy screens, `gate`, middleware and
Blender code-first boundary were reused rather than replaced. The owner resource
map's operations guide supplied the separate installed-versus-loaded proof and
resource-ownership checks. Those are process references, not upstream authority.

## Executable acceptance

- Browser: `python3 -m unittest discover -s tests -p 'test_browser*.py' -v`.
  A control-only change must count as progress; three executed no-op actions must
  stop; a matching title must not erase a failed execution, host escape, missing
  expected text or tick budget. Explicit model BLOCKED remains blocked.
- Search: `python3 -m unittest discover -s tests -p test_search_planning.py -v`.
  An invented source/time/query, malformed response, timeout, low confidence or
  private request must not execute or silently broaden a search. Original URLs,
  source failures and all duplicate variants must survive aggregation. Hidden
  injection/unread companions must not produce an answer through a clean copy.
- Gates: `python3 -m unittest discover -s tests -p 'test_gate*.py' -v`.
  Contradictory failed-provider approve labels escalate; deny is not weakened;
  advisory outcomes do not bypass host approvals.
- Existing routing: `python3 -m unittest discover -s tests -p test_plugin_middleware.py -v`.
  Manual models/effort, provider binding, disabled modes, request-shape support and
  concurrent-session isolation remain regression requirements, not new features.
- Full verification: `python3 -m unittest discover -s tests` and
  `python3 scripts/check_release.py`. The release scanner's pre-existing binary
  exclusions are not a claim that it inspected images/videos.
- Real DOM: `python scripts/browser_diagnostics_smoke.py --output evidence/ecosystem/browser-dom.json`
  with optional Playwright available, inside the operator's normal foreground
  resource lease. Uses installed Chrome and intercepts requests locally; no
  browser download, credentials, public request forwarding or provider calls.
  Includes control-only progress, scroll, no-op blocking and explicit BLOCKED.
  Closes its owned context/browser in finally. Synthetic choices do not measure
  Jev's decision quality or prove production-site behavior.

## Review and failure evidence

The new fixtures first failed on the missing browser/search interfaces. A
separate regression reproduced three unsafe strict-tiebreak outcomes: an approve
label combined with error, drift or fallback metadata was still approved. That
boundary now escalates those cases. These are constructed contradictory records,
not a claim that the live provider emitted them.

Self-review caught meaningful fragments/query-byte normalization, unread overflow
duplicates, post-action observation failures and vendor text-only stagnation.
The implementation and regressions preserve those boundaries. Local DOM QA uses
real input and live reads with synthetic decisions, not fabricated browser output.

The final DOM rerun also exposed a fixture-adapter mismatch after tightening the
vendor-stagnation boundary: its synthetic execution history omitted the inspected
vendor's `page_changed:false` receipts. Correct the adapter to carry that evidence,
not the production guard to accept unknown blocked reasons. The smoke invalidates
an older passed receipt at startup so a failed rerun cannot leave stale success.

Receipts are under `evidence/ecosystem/`. They prove the named code/DOM contracts,
not calibrated confidence, measured cost savings, faster task completion, security
certification, or an autonomous goal loop. No paid model evaluation was run.

## Installation and publication boundaries

Use the repository installer rather than editing installed copies. Root-only
installation does not grant other-profile edits, change gate/routing modes or
restart a gateway. Installed files, fresh-process availability, in-flight plugin
loading and merge/release state are separate facts. The operator must record the
actual installation check and exact pushed revision in their private handoff.
