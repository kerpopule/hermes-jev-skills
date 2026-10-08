# Open-PR review from first principles

## Scope and evidence

Reviewed all four open pull requests: #39, #40, #41 and #42. A local integration
candidate combines their exact heads with current main, retaining every Unreleased
entry. This is a tested candidate, not a merge to main, release or live fleet
installation. Self-review is not independent review.

The combined suite passes 1,473 tests on Python 3.9 and 3.11. The real installer
was previewed and run in a disposable Hermes home; installed design-choice and
local-goal APIs were exercised and the provider-neutral manifest verified.
Existing ResourceWarnings were emitted; no test failures. The sanitized execution
receipt is `evals/pr-first-principles/qualification-2026-10-08.json`.

## #39: design-choice reference

Fundamental requirement: the chosen design must be a complete, valid rendering
configuration. A probabilistic choice cannot repair an invalid preset, and a
high-confidence choice is not a visual-quality certificate.

The PR correctly lets code own presets, escaping, contrast, output bounds and
baseline fallbacks. Its enum intent already identifies the deterministic baseline,
so this example does not demonstrate that a paid model is necessary. Keep it an
explicit reference example, not an automatic routing feature. Its earlier browser
qualification remains historical evidence; this integration review did not rerun
or expand it. No new blocker found within that stated scope.

## #40: provider-neutral manifest

Fundamental requirement: installation must not demand an irrelevant credential.
The runtime accepts a provider choice; the manifest loader interprets required
variables conjunctively. Listing four keys would turn one valid provider into a
four-secret requirement, not solve the mismatch.

Removing the TypeSafe-only install requirement and retaining runtime key resolution
is the smallest correct change. The existing no-key path remains an explicit
unavailable-result path. The combined disposable installation confirms the changed
manifest ships. Recommend merging as the narrow fix for #36, not redesigning
credential storage or adding new schema requirements.

## #41: bounded local-goal pilot

Fundamental requirement: selection and execution authority are different contracts.
The selected ID must resolve to a caller-owned action, an exact current target and
an independently verifiable result. A label regex and high confidence are not
proof that a click is safe; a page can call a consequential button 'Continue'.

The pilot should remain opt-in. Its caller must prevalidate element actions and
enforce target scope, stale guards, approvals, deadlines and deterministic completion.
The module neither executes the action nor proves that a native UI task completed.
Reconciliation into public source is useful, but historical synthetic-engine
receipts do not qualify a production/native driver. Recommend merging the source
reconciliation with these limits, not promoting the pilot to a default executor.

## #42: evaluation and recovery

Fundamental requirement: a sent request may have incurred cost even if its response
or terminal outcome is missing. Absence of a result is not evidence of no send.
A recovery routine must refuse contradictory evidence rather than create another
request or a stronger quality claim.

This review found the prior verifier accepted a missing sent-row outcome, a sent
row labeled skipped, and inconsistent or missing attempt logs. The fix on #42
now checks local write-ahead attempt records against local and aggregate calls and
requires terminal outcomes for sent rows. Four cases were reproduced as failing
regressions before the repair; six offline corruption cases pass after it, alongside
the valid cumulative-budget continuation test. The #42 full suite passes 1,438 tests
on Python 3.9 and 3.11. No paid replay or uncertainty retry was performed.

This checks consistency of local artifacts, not authenticity against an attacker
who rewrites every artifact. A crash without a final receipt/report remains refused;
it is not silently recovered as a never-sent request. Earlier live results and cost
receipts remain unchanged. Merge this as measurement and recovery hardening, not
as a fix for classifier generalization (#25).

## The unresolved security problem (#25)

Separate two goals that the issue can otherwise conflate:

1. **Measurement quality:** frozen manifests, fixed questions/thresholds, explicit
   failures and clean denominators make claims reproducible. #42 addresses this.
2. **Attack recall:** a fixed probabilistic classifier misses attacks on unfamiliar
   distributions. The observed 322/1,051 withholding does not justify declaring
   this solved or tuning on the same exposed test set.

Rebuilding from the actual safety requirement leads to a capability boundary:
external text is evidence, never authority to expand permissions, reveal secrets,
change destinations or invoke tools. Screening may prioritize review or withhold
obvious attacks, but cannot authorize actions. Even a second model verifier is an
additional probabilistic check, not the missing deterministic boundary.

The next engineering experiment should first use offline executor-shaped fixtures:
feed an unflagged page instruction into a consumer, try a credential request,
a destination change, an invented action ID and a stale observation; require
refusal without any secret access or side effect. This tests the enforcement seam,
not just whether a classifier assigns a high score. The owning consumer must enforce
that contract; this library alone cannot secure every downstream agent.

If classifier recall improvement is still desired after that, define a new candidate
and acceptance criterion before accessing the untouched reserve or collecting a
new evaluation set. Keep exposed test data out of tuning and report false positives,
fail-opens and downstream outcomes separately. New paid calls require fresh scoped
budget approval. No further spending is implied by the earlier cap.

## Merge decision

At initial read, #39-#41 had green exact-head CI; #42 had nine successful checks
and one queued duplicate macOS job. The recovery update creates a new #42 head,
so its checks must be evaluated again. The local combined candidate resolves
changelog-only conflicts but does not modify main or the original PR histories.

Recommended order: #40, #39, #41, then #42, preserving all Unreleased entries and
rechecking each resulting merge candidate. Steve's exact merge approval is still
required. No automatic release, live install, gateway restart, classifier tuning or
new provider call is part of this review.
