# Live scouting and failure-triage pilot, 2026-09-28

Historical v1 results below are unchanged. Larger, stronger-comparator validation is now available in [independent-v2/SCORECARD.md](independent-v2/SCORECARD.md). It retains NOT_PROMOTED and finds stronger deterministic triage better than semantic advice. A/B are now observed development/regression data, never fresh validation for later changes.

## Decision

**NOT_PROMOTED.** Keep both helpers opt-in/advisory. The failure classifier improved on this synthetic pilot, but scouting still misses relevant evidence and adds net context and latency. Do not replace ordinary search, delete non-shortlisted evidence, suppress test failures, or lower confidence thresholds to improve these scores. No automatic feature activation, compaction, gateway restart, main merge or release is authorized by this result.

## Provenance and reproducibility

Base: `ab707143d6dd8c4d7b73695b016c5a87aff2decb`, branch `donna/file-scout-triage`. Commit `8436c06` froze the synthetic corpus, split, labels, metrics, promotion bars and bounded runner BEFORE provider scoring. `live-v1-freeze.json` pins corpus and protocol bytes. Existing `transfer-v1` fixtures were regression data and were not reused as heldout.

Panel A has 12 scouting tasks (three excerpts each) and 24 failure records; it became development/regression data after the first run. Panel B has 12 distinct scouting tasks and 24 previously unscored failure records. B was run only after the A-driven code fix and its tests passed, and was not used to tune thresholds or code. It is heldout synthetic validation, not an independent human-authored or real-project distribution. Its error families and paraphrase structure intentionally match A, so it does not establish general NLP robustness.

Three purposeful batches: `a-initial`, `a-regression`, `b-validation`. Each made 46 logical provider requests, all returned model `jev-1.13.0`, with no missing usage and no recorded timeout, malformed response, drift or limiter fallback. Production `public_advice` -> `decide` -> guarded `client` was used, with existing credential resolution and privacy screening. The benchmark transport meters the existing TypeSafe HTTP transport; it does not replace the provider with a scripted oracle. Offline fault injection is separate.

Recompute without network:

```
python3 scripts/summarize_file_evidence_live.py
python3 -m unittest discover -s tests -p 'test_file_evidence*.py'
```

An explicitly approved repeat uses `python3 scripts/bench_file_evidence_live.py --panel A --tag UNIQUE --live-approved-public-synthetic`. Do not call a repeat heldout. Do not overwrite existing receipt tags. The runner verifies corpus/protocol hashes, requires the TypeSafe provider, refuses alternate model/base/proxy overrides and disabled fleet limits, reserves attempts before network calls, and stops at 100 attempts per process, 600 recorded across the corpus, 600 seconds, or five failures. No client retry is requested. Logical attempts do not audit lower-level socket retransmissions or provider billing events. The 600-request/16,000-wire-byte envelope reserves a conservative $0.4032 input-list-price estimate; this is not a provider hard billing cap. Existing fleet daily/RPM brakes remain active.

## Scouting results

The predeclared baseline is local lexical top-1. The enhanced pipeline is the UNION of that shortlist and individually approved public excerpts receiving a high-confidence relevant decision. This union is an evaluation composition over existing APIs, not a new automatic search hook. Shortlist sizes differ intentionally and are reported. The corpus stresses synonyms and keyword distractors; it is not representative of ordinary repository search.

| Metric | A initial local | A initial enhanced | B fresh local | B fresh enhanced |
|---|---:|---:|---:|---:|
| Relevant hits / total | 2/12 | 9/12 | 1/12 | 10/12 |
| Recall | 16.7% | 75.0% | 8.3% | 83.3% |
| Precision | 16.7% | 47.4% | 8.3% | 47.6% |
| Selected files | 12 | 19 | 12 | 21 |
| Mean shortlist / task | 1.00 | 1.58 | 1.00 | 1.75 |
| p50 wall ms | 2.45 | 530.57 | 2.72 | 536.47 |
| p95 wall ms | 3.82 | 653.14 | 6.45 | 658.40 |

A regression reproduced the same selection metrics. Semantic-only precision was 100% on A and 90.9% on B; union retains deterministic distractors rather than falsely claiming semantic purity. Exhaustive eligible-manifest recall was 100% in both panels and every eligible reference remained recoverable.

For fairness, a supplemental AFTER-THE-FACT local run with the actual default `top_k=8` yields 5/12 hits from 20 selected files on A and 7/12 from 25 on B. This was not the frozen comparator and is not a retuned benchmark. Small top-1 budgets depress the main baseline; the improvement must not be generalized to all deterministic search. Reading all three eligible excerpts costs no provider request and misses none in this tiny corpus.

Misses: A `a-clock:1`, `a-multiple:0`, `a-multiple:1`; B `b-page:0`, `b-bound:0`. All had Jev's highest choice `relevant`, but calibrated confidence below the existing 0.85 threshold (A: 0.48, 0.76, 0.81; B: 0.23, 0.69). Raw choice probability is not calibrated confidence. Thresholds were NOT relaxed. Conservative abstention plus exhaustive recovery is preferable to a benchmark-specific threshold adjustment. The critical/multi-evidence no-miss bar fails.

## Failure triage results and repair

Each panel has 16 known symptoms and 8 unknown records, including six misleading mentions. These are symptom categories, NEVER root causes.

| Metric | A initial local | A initial enhanced | A repaired local | A repaired enhanced | B fresh local | B fresh enhanced |
|---|---:|---:|---:|---:|---:|---:|
| Accuracy | 11/24 | 20/24 | 15/24 | 24/24 | 15/24 | 24/24 |
| Correct known coverage | 7/16 | 16/16 | 7/16 | 16/16 | 7/16 | 16/16 |
| False certainty on unknown | 4/8 | 4/8 | 0/8 | 0/8 | 0/8 | 0/8 |
| False certainty on misleading | 4/6 | 4/6 | 0/6 | 0/6 | 0/6 | 0/6 |
| Abstentions | 13 | 4 | 17 | 8 | 17 | 8 |

The local classifier matched error names in negated observations, expected exceptions, previous-run history and quoted test strings. Because the deterministic path was confident, Jev could not repair these false positives. Added conservative local abstention BEFORE classification or outbound requests. Four failure assertions reproduced RED before the repair and now pass. Ordinary positive observations, including an assertion discussing a string, have contrast tests. Also fixed and reproduced an empty-file reference that was created with an invalid end-line of zero; empty evidence now safely round-trips as empty text.

Fresh B local p50/p95: 0.316/1.003 ms. Enhanced p50/p95: 0.286/278.310 ms. The enhanced median is dominated by local-only classifications; sub-millisecond differences are timer noise, not a speedup. A initial enhanced p95 was 185.874 ms, A repaired 206.105 ms. All nonzero supplied exit codes and failure flags survived, with `proven_root_cause=false`. No actual test was represented as fixed by a category label.

Conservative whole-log abstention can also reject genuine later failures after historical text. Unfamiliar negation, adversarial phrasing and domain-specific errors remain risks. This pilot does not justify changing that conservative tradeoff.

## Actual usage, estimated cost and context accounting

Total: **138 logical attempts and 138 responses, 57,550 provider-reported input tokens**. Stored per-call list-price estimates sum to **$0.00241705** using the repository's recorded $0.042/million input rate; output is assumed free by that price table. This is a LIST-PRICE ESTIMATE, not a current quote, charge receipt or verified invoice. The existing decision field called `cost_usd` is computed by the repository's ledger, not evidence of actual billing. No credential, header, raw response, private repository excerpt or real log was retained in receipts.

| Batch | Input tokens (provider reported) | List-price estimate | Request p50 / p95 ms |
|---|---:|---:|---:|
| A initial | 19,168 | $0.00080503 | 160.16 / 216.16 |
| A regression | 19,168 | $0.00080503 | 166.96 / 241.18 |
| B validation | 19,214 | $0.00080699 | 168.47 / 270.47 |

Selected excerpt context on B: 1,274 bytes versus 1,832 bytes for exhaustive reading. Using explicitly heuristic `ceil(UTF-8 bytes/4)` per excerpt, these are 327 versus 472 frontier tokens. This is NOT an installed tokenizer or measured frontier bill, excludes evidence-reference and response overhead, and is optimistic for the enhanced pipeline. B's scouting requests consumed 14,378 provider-reported input tokens. Thus estimated combined input is 14,705 versus 472 for reading all excerpts: **14,233 more tokens, not savings**. A combined input is 14,619 versus 440, or 14,179 more. Provider tokens and heuristic frontier tokens use different counting bases; no dollar equivalence or generic savings is claimed.

## Verification, source installation and remaining boundary

`python3 -m unittest discover -s tests`: 1,379 passed in 63.884 seconds on this macOS Python 3.9 runtime. The 24 evidence-specific tests include timeout/malformed/low-confidence fallback, privacy and injection egress prevention, binary/invalid UTF-8, symlink/hardlink/root escape, oversize and budget handling, empty-reference recovery, error preservation, live-runner bounds, receipts and secret-free logging. Unit transports are synthetic only. `scripts/check_release.py` passed; the existing unchanged dashboard PNG remains explicitly unscanned. No new binary artifact is shipped.

The real installer was exercised preview-first in a disposable home and installed CLI tests passed. Owner-approved root-only source refresh was then performed without configuration edits. Preview and install report a deliberate warning/exit 1: the root `bin/jev` symlink belongs to canonical main, outside this review checkout. The installer preserved that link and copied the root `hermes-jev` and `hermes-handoff` module trees. Every source-module byte was compared with both installed trees; all 40 config-file hashes and the canonical CLI symlink remained unchanged. Existing profile links were not edited. Installed-module CLI invocation retained exit code 7, returned process exit 1 and abstained on a negated TimeoutError. This is verified partial installation with a known CLI ownership warning, NOT a clean full-install exit.

Root shared plugin copies now contain the candidate modules; existing profile links inherit those bytes. The normal canonical CLI still comes from main. To exercise the candidate explicitly use this worktree's `bin/jev` or its installed module path; there is no automatic scouting/triage hook or activation toggle changed. No gateway restart was performed. Full canonical CLI rollout follows review/merge authority, not benchmark permission.

Source skill edits remain blocked by this execution environment's explicit developer instruction: "do not create or edit skills". The task permitted skill integration only if allowed. That restriction was not bypassed by editing installed copies or delegating. Source/installed skill content remained unchanged; complete caller guidance is in `docs/file-evidence.md`. Matched skill-facing integration therefore remains a concrete rollout gate.

PR #26 and its separate credential gate were checked read-only and left untouched. Main was not merged; concurrent work was not cherry-picked, replaced or reset. Promotion fails on scouting recall/critical misses, net context benefit, undersized synthetic sample and unverified actual billing, despite promising failure-triage results. A larger independent realistic public corpus is required before any promotion, not more repeats of these now-known fixtures.
