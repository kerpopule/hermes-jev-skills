# Jev nightmare qualification and Switch-inspired replay

## Scope and approval ledger

Steve asked to adopt useful Switch patterns, nightmare-test Jev Skills, improve defects and address open PRs. This pass permits local synthetic tests, bounded repairs, publication to the already-authorized Jev repository and root-only installation. No new spending is authorized. No live customer data, paid model calls, credential handling, routing-mode changes, main/release merge, gateway restart or other-profile writes are part of this work.

Completion test: reproduce defects before repair; pass the hostile corpus and full package/dashboard regressions; verify trace integrity and changed-definition refusal; test the open PR on its own branch; run the release/privacy guard; install root-only from the canonical source checkout; exercise fresh installed code; push and read back exact branch heads. Remote CI and existing loaded processes are separate claims.

## Resources actually reused

1. `osolmaz/switch` core pick/replay and rubric-judge concepts: borrow explicit typed outcomes, bounded cases, definition bindings and model-free replay. Do not add the TypeScript runtime or copy code. Our replay re-exercises the current validator, rather than treating a recorded state as evidence of action execution.
2. Existing `jevkit/client.py`, package tests, release scanner and installer: fix the shared validation boundary instead of changing thresholds in individual skills. Preserve valid empty-criterion shorthand and missing score distributions because regression tests demonstrate they are supported contracts.
3. Current PR #56 and its companion tests: repair the actual main-merge conflict and add a failing empty-directory-flood case before bounding inspection. Keep the companion default-off and host-code warnings intact.

## New artifact

`jevkit/decision_trace.py` and `scripts/nightmare_contract.py` provide an explicit offline wire-contract corpus and trace/replay. No keys, network calls, model calls, subprocess execution or action transitions occur in the validator path. The corpus covers noul, choice, score and box questions, valid controls, wrong types, malformed/deep JSON, duplicate members, unsolicited answers, bad numeric values and invalid distributions.

Traces contain opaque fixture IDs, definition digests and normalized outcome digests or refusal codes, not raw fixture bodies. Hashes bind content but do not establish secrecy, authenticity, semantic correctness or external execution. Do not feed owner/customer/private records into this harness. It is for synthetic/public fixtures only and is never auto-enabled or auto-logged.

Run from the repository:

```sh
python3 scripts/nightmare_contract.py --record evidence/nightmare/new-trace.json
python3 scripts/nightmare_contract.py --replay evidence/nightmare/new-trace.json
python3 -m unittest discover -s tests
python3 scripts/check_release.py
```

Record refuses to overwrite an existing evidence file. Replay refuses changed/reordered/missing definitions and corrupted trace digests. Current validator behavior changes are explicit outcome diffs. No replay result can approve a real action or promote policy thresholds.

## Reproduced repairs

- Duplicate JSON members could silently replace a previous answer or answer map. Strict decoding now rejects ambiguity.
- Huge numeric answer/confidence/probability values and huge token-usage values could raise uncaught `OverflowError`. Invalid decision numbers now produce typed refusal; unrepresentable usage counts become unavailable.
- Unicode pseudo-digit score keys could raise uncaught `ValueError`; noncanonical score keys are rejected and malformed legend keys are ignored safely.
- Present but non-object score distributions could silently become an unverified missing spread. They now fail validation. Truly absent/null spreads remain supported and marked unverified.
- Extra unsolicited answer IDs were silently ignored. The answer ID set must now exactly match the requested set.
- A custom transport could bypass the native response byte limit. The shared validator enforces the same bound on every path and rejects invalid transport return types.
- Distinct question IDs that stringify identically could silently overwrite a definition. Collision now fails before transport.
- PR #56 bounded artifact files/depth but not empty directory breadth. Total artifact entries now have an explicit cap in both shipped companion copies.

`tests/test_nightmare_contract.py` exercised actual `client.ask` with injected synthetic wire responses before and after repairs. Existing regression tests caught an over-strict proposed empty-criterion rejection; that proposal was removed rather than breaking the supported API.

## Verification receipts

- Final hostile corpus: 863 cases, comprising 104 valid controls and 759 invalid responses, with zero expectation mismatches; all 863 replayed without outcome changes. The final bound trace is `evidence/nightmare/qualified-trace.json`. Earlier `trace.json` and `trace-final.json` preserve development corpus iterations, not final qualification.
- Final package suite: 1,690 tests on each of Python 3.9, 3.11 and 3.13. Python 3.9 ran without skips; each newer interpreter skipped five PyYAML-dependent checks. See the per-interpreter logs and `test-matrix.json`.
- Routing dashboard: 71 tests passed with the existing Python 3.9 environment. The first 3.11 attempt failed for missing PyYAML, not an application defect. No dependency install or service restart was needed.
- PR #56: `9db2ad8ecfde67a28256de797e3e8ac0cc1ff550`, 1,649 Python and 11 Node tests passed locally. Exact-head CI succeeded and GitHub readback reports MERGEABLE. The PR remains open and default-off; its update was read back after publication.
- The release scanner passed for text files. It flags pre-existing binary evidence as unscanned; this pass introduces no new binary files and makes no claim that the scanner inspected pixels.
- `scripts/verify_nightmare_install.py` checks installed bytes, fresh installed-module imports, duplicate-member refusal, pure trace/replay and a deterministic CLI path. Installation and source commit evidence is recorded separately, never inferred from a successful copy alone.

## Limits

This is structural/adversarial contract qualification, not a fresh paid-model benchmark, a measured reduction in semantic mistakes, independent review or proof of general prompt-injection resistance. Existing routing, memory, search, GUI/browser, gate, privacy, budget, installer and handoff regressions are rerun, but their mocked/provider fixtures are not new live-provider quality measurements. No thresholds, provider defaults or live modes are promoted.

The open Pomerado PR remains separate from the installed nightmare branch. Updating and testing that PR is not approval to merge it or activate integrations. Preserve both worktrees and all pre-existing unrelated files.
