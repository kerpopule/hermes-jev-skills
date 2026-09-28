# Evidence caller integration audit and validation handoff

## Actual supported callers

The review branch exposes `jev evidence scout`, `jev evidence read`, and explicit approved `jev evidence advise` through `jevkit/cli_evidence.py`. `jevkit/cli.py` dispatches the existing `triage --preset test-failure` surface; `jevkit/triage.py` dispatches its Python counterpart to `test_triage.classify`. The CLI deliberately overrides semantic flags to local-only. Python callers must own the approval booleans outside untrusted log data. No Hermes tool-start hook, shell-output interception, automatic reranker, routing change, or compaction is registered. Installing modules alone does not make agents use these helpers.

For actual pipeline integration, run the real test command first, retain its original log and status, and feed bounded local JSON containing `error` and `exit_code` to the triage CLI. A supplied nonzero status makes the triage command return 1; that is not the original process status and must not replace it in a CI exit-status contract. The report retains the original status. The CLI cannot request semantic classification through log-injected flags. See the real subprocess tests in `tests/test_file_evidence.py` and the disposable installed-module CLI test in `tests/test_file_evidence_install.py`.

Default policy is deterministic-first and **skip semantic scouting**. The prior live pilot added net context and missed critical evidence; do not spend another provider round to classify a tiny manifest that can simply be read. Even larger manifests should begin with lexical/identifier search and exhaustive recoverable references. Optional semantic help is advisory, never proof of absence. The independent benchmark compares stronger BM25 and extended diagnostic signatures before considering any broader integration.

## Batch and crossover experiment

`bench_file_evidence_independent.py` uses multi-question requests only in the PUBLIC benchmark. Each opaque question targets one screened excerpt; the existing decide/client/limiter and exact confidence/margin rules apply. Nothing adds a batch endpoint to the installed plugin. The experiment retains verbose audit responses locally and measures both verbose and compact caller envelopes. Fewer HTTP calls do not themselves prove fewer input tokens, better accuracy or net savings. Shared-state interference, screens and eight-line snippet truncation are measured limitations.

## Verified deployment distinction

At the session's initial audit, review HEAD was `2357d2f1ddc99095f96da57e1ef3c05cb37e5c50`, canonical main was `89b073f`. Root installed plugin modules `file_evidence.py`, `test_triage.py`, `cli.py`, and `triage.py` matched review-source SHA-256 values. Both normal CLI symlinks resolved to canonical main, whose source lacked the new evidence modules. This is deliberate, unresolved candidate-versus-canonical skew, not a clean full rollout. Explicit worktree CLI invocation is the truthful candidate path. No reinstall, main merge, symlink retargeting, configuration edit or gateway restart was performed in this continuation. New benchmark scripts do not require a shared install.

Current execution instructions still explicitly prohibit skill creation or edits. Matching SOURCE skill integration is therefore blocked, not done; installed skills were not edited as a workaround. This document is caller guidance, not a substitute claim that the skill rollout happened. Review/merge, source skill authorization and deliberate canonical CLI reconciliation remain rollout gates.

## New validation provenance

`evals/file-evidence/independent-v2/` contains 32 PSF-licensed public CPython functions, eight multi-evidence tasks, 24 actually executed isolated Python failures and 40 clearly labeled authored diagnostic fixtures. Freeze hashes pin source, protocol, runner, policy and stronger comparator inputs before live scoring. No A/B repeat is called fresh. Author-selected labels are not independent human annotation; authored multi-language examples are not real captured CI. The public standard-library source and actual failure executions provide more realistic input than the prior three-excerpt synonym panels, but generalization remains bounded.

Pre-scoring verification: 1,387 unit tests passed; `scripts/check_release.py` passed over 251 text files. The unchanged dashboard PNG remains explicitly unscanned. The full suite includes real CLI and disposable installer smoke tests. Provider accuracy, usage, latency, context crossover and the promotion decision will be reported in the independent scorecard, not inferred from these offline checks.
