# File evidence candidate verification

Historical offline verification at `ab70714` follows. Superseded for current live measurements and installation status by [the live scorecard](../evals/file-evidence/LIVE-SCORECARD.md): 138 real provider requests, fresh panel B validation, 1,379 passing tests and a verified root-plugin-only refresh with the canonical CLI left unchanged. Both helpers remain NOT_PROMOTED. Source skill edits remain forbidden by this execution environment.

This is a review-branch candidate, not an activated fleet feature or a release.

## Executed checks

- `python3 -m unittest discover -s tests`: 1370 tests passed on the available macOS Python 3.9 runtime. The final run took 63.585 seconds. Tests use disposable fixtures and scripted transports; no paid model benchmark ran.
- `python3 scripts/check_release.py`: exit 0, 224 text files scanned at that point. The existing unchanged dashboard PNG was explicitly unscanned. No new binary artifacts are in this change.
- `git diff --cached --check`: exit 0.
- `scripts/eval_file_evidence.py` reproduced `evals/file-evidence/transfer-v1-regression.json` byte-for-byte.
- `tests/test_file_evidence_install.py` runs the real repository installer with `--hermes-root-only` in a disposable home, previews first, asserts no warnings/config changes/profile links, compares installed module bytes, and invokes the installed CLI. The intentional failing-test fixture returns exit 1 and retains original test exit code 7.

The production command gate is unchanged. One existing test's assumption that every `TMPDIR` is automatically classified as scratch was replaced with an explicit fixture scratch root; production risk/approval behavior was not modified.

## What the measurements do and do not say

The first synthetic transfer has 5 scouting tasks with 7 relevant files and 12 failure examples. Lexical shortlist recall is 4/7; exhaustive eligible-manifest recall is 6/7. Two lexical misses remain recoverable; a private-path item is explicitly unknown. This is evidence AGAINST replacing normal search with the shortlist.

Initial failure-taxonomy accuracy was 10/12, with false certainty on 2/5 unknown examples. After the conservative negation/example abstention change, all 12 same-fixture regression labels match. Those fixtures are now development/regression data, not a held-out validation set. Unit-test scripted Jev answers are contract fixtures, not an accuracy measurement. New features remain opt-in and NOT_PROMOTED.

No live accuracy, p50/p95 latency, token savings, or dollar savings were measured. The frozen protocol requires a new held-out corpus and baseline comparison before promotion. A proposed pilot is at most 200 reviewed public/synthetic excerpts, one bounded request each, about 2500 input tokens per request. Using the repository's recorded list rate of $0.042 per million input tokens gives an illustrative $0.021 input estimate. This is NOT a current provider quote or billing cap; confirm current pricing and require an explicit maximum spend (suggested $0.10) plus approval of the exact corpus hash before any paid run. A key existing locally is not approval.

## Scope and outstanding review

No merge to main, live/shared installation, profile activation, gateway restart, credential change, PR #26 change, release, or automatic compaction was performed. The established scoped installer was verified in isolation to avoid overwriting concurrent shared work.

CLI help, README, operating documentation, evaluation protocol and CHANGELOG were updated. Skill files were deliberately left unchanged because this execution environment explicitly prohibits creating or editing skills. Any skill-facing rollout remains a separate integration review; the docs contain the complete proposed caller workflow without making it mandatory.
