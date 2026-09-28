# Independent-v2 final verification

Review branch: `donna/file-scout-triage`. Freeze commit: `765061450092fdea64a9fa8d2f9dbdaf648c4fd4`. This continuation changes evaluation/recount scripts, tests, public evidence and documentation only. No frozen corpus, protocol, comparator, production helper or policy was changed after scoring.

## Exercised checks

- Full `python3 -m unittest discover -s tests`: **1,393 tests passed in 69.417s**, exit 0, after final accounting tests were added.
- Explicit `test_file_evidence.py`: 14 passed, including real CLI subprocesses, exact outbound schema with scripted transports, link/tamper/privacy guards, drift/margin and malformed/timeout abstention.
- Explicit `test_file_evidence_install.py`: one passed. Actual `install.py --hermes-root-only --hermes-home <disposable> --check` and install succeeded; installed-module CLI worked from outside the source tree; root and sentinel-profile configuration stayed unchanged. This is a disposable install, NOT a shared fleet refresh.
- Actual `python -I -S -c 'assert 2 == 3'` failed with status 1. Its real stderr and status were piped via JSON to the candidate triage CLI. CLI returned 1, category assertion, preserved exit 1, and `sent_to_jev:false`, even with semantic flags supplied in JSON.
- Offline reconstruction matched all 318 retained request-body SHA-256 hashes and lengths. Summary reproduces exactly; tests reject duplicate receipts and incomplete case sets, and preserve missing-response reservations as unknown usage rather than silently dropping them.
- Release privacy scanner passed over all newly staged public/synthetic JSONL receipts and reconstructed bodies. Its unchanged existing `docs/images/model-routing-dashboard.png` binary warning remains; no new binary is included. `git diff --check` passed.

These tests are regression/contract checks, not new live accuracy evidence. Full live results and limitations are in `evals/file-evidence/independent-v2/SCORECARD.md`. Public Python source retains PSF licensing and provenance. Raw responses cannot be reconstructed and are not claimed retained.

## Review and installation boundary

Reviewed candidate evidence/triage/CLI implementations for explicit manifests, no-follow root/file handling, bounded full-file privacy checks before snippets, pinned hashes, preserved status, caller-owned consent, local-only triage CLI, and existing policy/client/limiter use. Reviewed post-freeze scripts for offline-only reconstruction, hash integrity, original evidence preservation, missing-token accounting, nearest-rank metrics and no semantic promotion from a weak baseline. Receipt reconstruction intercepts before key, limiter, ledger or network access. No production threshold was tuned against this corpus.

Read-only final audit found canonical main at `89b073fc325992df734eb850f55ef2a5b34e45ac`. Root-installed `file_evidence.py`, `cli_evidence.py`, `test_triage.py`, `cli.py`, and `triage.py` match review-source bytes. Canonical main lacks the three new evidence modules. Both normal CLI links resolve to canonical main's `bin/jev`; candidate CLI verification uses the review checkout explicitly. This preexisting version skew is documented, not silently reconciled by changing shared state.

Current execution instructions explicitly prohibit skill creation/edits. Source skill integration therefore remains blocked; installed copies were not edited as a workaround. The rollout gate is permission to make matched source-skill changes, review/main integration and deliberate canonical CLI/install reconciliation. No main merge, shared reinstall, retarget, gateway restart, routing change or production promotion occurred. Batch composition and stronger comparators remain benchmark-only. The justified disposition is advisory-only, deterministic-first, skip semantic scouting, NOT_PROMOTED.
