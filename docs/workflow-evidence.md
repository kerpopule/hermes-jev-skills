# Workflow evidence: what to default to, and what not to infer

This is a decision guide, not a claim that every feature is active or generally faster. Preserve fleet-owned approval and privacy rules.

| Workflow | Evidence available in this repository | Operational conclusion |
|---|---|---|
| Labelled UI actions | [Native A/B receipt](image-action-demo.md): both image coordinates and elements completed the fixture; capture/inference dominated the screenshot path | Elements remain first choice for labelled controls. Image coordinates remain explicitly authorized, executor-checked, opt-in evidence |
| Layout/type/palette presets | [Design-choice demo](design-choice.md): local contrast and copy checks plus a closed preset choice; offline fallback; no measured design-quality gain | Code defines valid presets. Jev may select among them; never render arbitrary model HTML/CSS or claim the fixture proved better taste |
| Blender | [Code-first policy](blender.md): local geometry/material/encoded-output checks; bounded read-only advice only for unclear expensive probes | Known-ahead builds and routine checks make zero Jev calls. Preserve warm-session ownership and operator production rules |
| Video QA pilot | [18-frame trial](video-qa-trial.md): 12 defective frames flagged, six clean frames cleared in a guarded synthetic recheck | A useful small pilot, not general calibration, acceptance or proof of avoided review |
| General video QA | [33-frame holdout](../evidence/video-default/REPORT.md): two tiny-text defects received low-confidence clears but were referred; no confident clears or general review-time saving | Default **screening/review planning**, not auto-clearance. Retain audits, call caps, low-coverage/full-review fallback and local-only private-media handling |
| Web screening | [Scorecard](../evals/web-screen/SCORECARD-2026-09-26.md), [heldout](../evals/web-screen/HELDOUT.md), [follow-up](operational-follow-up-2026-10-01.md) document attack detection, benign losses, local fallback and limits | Local deterministic screen plus bounded classifier. Evaluate the security/availability trade-off; do not generalize a synthetic set into a production false-positive rate |
| Model/effort routing | [Plugin](../hermes/plugin/hermes-jev/__init__.py), middleware regression tests, and [routing skill](../skills/jev-model-routing/SKILL.md) | Available is not active. Preserve manual choices. Chat Completions uses top-level `reasoning_effort`; Responses uses `reasoning.effort` only inside an already-enabled object |

## Try a decision workflow offline

From the repository root, run:

```sh
python3 -m scripts.offline_workflow_demo
```

This replays two canned responses through the existing [design-choice example](design-choice.md) and the real response validator. A confident answer selects `editorial`; an uncertain answer falls back to the original `product` preset. The output reports both paths and zero model calls. It does not read credentials, contact a provider, or measure accuracy or latency. The preview-first pattern is inspired by [TypeSafe Jev Workflows](https://github.com/chaitanya-y/typesafe-jev-workflows); no upstream code is used.

## Code-search comparison

The [code-search baseline](code-search-baseline.md) records three source-location questions and verified excerpts on a pinned public tree. It is a local baseline, not a JevGrep result.

## Question design is an evaluation, not an automatic upgrade

The old video holdout is now development knowledge and must not become a newly claimed independent confirmation set. A fresh [offline A/B preparation](video-qa-question-ab.md) freezes the current composite question against independent presence/readability/occlusion/elements questions. It does **not** change the production question or floor, run paid calls, or promote a candidate.

Neither confidence nor a syntactically valid answer certifies semantic correctness. Compare unsafe clears and useful coverage on fresh cases, include all fallback and verification cost, and retain mandatory acceptance review.

## Keep the layers separate

Local encoded-media checks, including private Cinetic-inspired overlays, can complement these workflows. They are separately versioned dependencies, not proof of an engine migration or files bundled in this public repo. Fleet reports should list such dependencies by source revision/fingerprint and avoid copying customer content into public evidence.

Shadow reports are counterfactuals. Read the [measurement contract](measurement-and-provenance.md) before interpreting cost, avoided tokens or a claim that installed files match a release.
