# RightNow evaluation, not a global Jev replacement

## Implemented contract

`jevkit.rightnow.ask` explicitly calls the API-key-page public `POST https://api.rightnowai.co/v1/decisions` with `rightnow-decision`. This is NOT the earlier rnsk-key API, a chat model, a writer, a transcription engine or a general tool executor. No SDK/vendor installer was run. Existing Jev provider resolution, tools, hooks, settings, model defaults and caller thresholds are unchanged.

The adapter preserves question instructions and criterion descriptions; it refuses >32 questions, >10 choice options, >4 score levels, boxes and structured instructions rather than truncating them. It uses the existing strict response validator plus exact cost/model and confidence/distribution checks. Redirects, partial refusals, unknown answers, nonfinite charges and late replies fail closed. It has no retries, cross-provider fallback or automatic key storage. Transport tests are offline; they are not real HTTP failure-injection evidence.

`rightnow_media` inspects local bounded immutable bytes before building inline image, WAV, PDF or MP4 state. Optional Pillow, pypdf and ffprobe must already exist. It does not add remote URL fetching, upload infrastructure or a background watcher. Single media family only; maximum four images or one other file. Formats are deliberately narrower than vendor advertising. Media preparation is NOT useful-perception or privacy certification.

## Reproduce without provider calls

```bash
python3 -m unittest discover -s tests -p test_rightnow.py
python3 scripts/rightnow_eval.py
python3 scripts/rightnow_benchmark.py
python3 scripts/rightnow_scorecard.py evidence/rightnow/text-smoke.jsonl evidence/rightnow/matched-media.jsonl
```

The last command is pure offline analysis of retained synthetic replies. A live run additionally requires the person's exact data/spending scope, an explicit finite cap and a privately supplied `RIGHTNOW_API_KEY`. Never paste a key into command arguments. `--prior-reserved-usd` carries the full reservation from earlier runs in the same approval. The guard is a conservative client-side reservation, not a provider account limit or an invoice. Parallel separate invocations do not share a ledger; do not run them under one cap. Failures retain their reservations; no top-ups or retries.

## Observed results, 2026-11-18

See `evidence/rightnow/scorecard.json` and both immutable first-run journals. The server reported `rightnow-decision-3.0.0`; matched Jev replies reported `jev-1.14.0`.

- **Direct matched text: 34/34 correct each.** 26 yes/no examples cover the 13 existing skill families, plus six bounded menus and two two-level scores. RightNow p50 414.5 ms, p95 521 ms; Jev p50 79 ms, p95 324 ms. These are sequential small-sample observations, not load/endurance benchmarks or evidence for all real skills.
- **RightNow reported text charge $0.00552828.** The comparison Jev estimate is $0.000428904 using the repository's configured $0.042/million input-token rate. Jev did not return an invoice charge. Therefore an exact billing comparison is unverified; this run does NOT establish 'cheaper'. Cache state is not controlled, no external price guarantee is made.
- **Original nested-negation cohort: RightNow 19/26, Jev 20/26.** The pilot asks whether an entire interrogative statement is false, which is a poor semantic rubric. Preserve it as a prompt-robustness failure, not a held-out quality comparison. The direct cohort fixes wording and changes negative evidence; it is deliberately easier, not a repaired benchmark with the failures hidden.
- **Media: 8/8 obvious positive/negative controls**, two each for image, readable-text PDF, tone/silence PCM audio and silent two-second video. RightNow reported $0.03937419 total, p50 619.5 ms, p95 959 ms. No realistic speech, tiny-text, brief flicker, scanned-document OCR, multi-person, adversarial or ambiguous held-out media quality has been accepted. Jev was not matched on media.

Noul Brier scores are descriptive only. Raw probabilities and choice confidence are NOT calibrated safety or correctness probabilities. Zero errors in this tiny corpus does not certify production reliability.

## Existing skill compatibility, all 13 families

Each family has a synthetic yes/no pair in `rightnow_eval.py`, not execution of its production user path. The native Jev calls remain unchanged.

| Family | Potential bounded use | Migration limitation |
|---|---|---|
| jev-blender | Select a review from geometry diagnostics | Keep deterministic geometry and engine; no box/coordinate replacement |
| jev-browser-use | Rank a prevalidated fresh DOM menu | >10 choices incompatible; no click permission inferred |
| jev-compaction | Retention decisions on synthetic/public turns | Long option sets need separate evaluated decomposition; private transcripts excluded |
| jev-computer-use | Bounded labeled action table | Screenshots new scope; >10 options incompatible; stale/approval gates mandatory |
| jev-frontier-work | Difficulty/seat advisory | Existing paid-seat quotas and authority unchanged |
| jev-mailbox | Synthetic mail-routing classification | No actual mailbox/customer disclosure authorized by this test |
| jev-memory | Relevance classification | Private memories remain excluded; prompt injection checks retained |
| jev-model-routing | Capability/fit advice | Protocol compatibility remains local; pool calibration and shadow acceptance absent |
| jev-search | Public result relevance | Existing screening and origin handling unchanged; not a substitute for verification |
| jev-setup | Public contract diagnostics | Never send credentials; existing secret-store flow unchanged |
| jev-skill-select | Small candidate relevance menus | Larger inventory menus need explicit separately qualified decomposition |
| jev-social-research | Evidence/relevance routing | No posting, source-truth authority or private audience data |
| jev-video-qa | Local-first media-review triage | Existing image transport is different; final full review/audits remain required |

## Installed Co-Agent caller inspection

The actual installed desktop selector permits 24 action candidates PLUS reobserve/abstain, whereas this public adapter permits 10 options total. Phone tables also can exceed 10. Do not silently truncate or split a selector table. Browser extension `browser_run` uses local unique DOM matching and its own scope/staleness/policy gates; it is not evidence of a Jev provider seam.

`evidence/rightnow/native-gate.json` exercises the **actual installed native selection gate** using a recorded real RightNow answer on a synthetic action table: answer shape accepted, low confidence, stale, redirected, unknown and consumed cases refused. No model request was made by this replay, no approval broker or action was executed, and no real phone/desktop/extension end-to-end path is claimed. This is contract compatibility for a small table only.

## Optimization policy and new skills

Use `rightnow-evaluation`, `rightnow-media-triage` and `decision-portfolio-optimization`. Start with deterministic local schema/hash/geometry/audio checks. Use direct positive questions and genuinely contrasting evidence rather than wrapping a question in 'is this statement false'. Include explicit uncertainty and preserve failed evidence. Then use a bounded provider decision only where it avoids more expensive work; refer uncertainty to full review. Never route every frame or every voice packet to a cloud decision call.

Best next candidate is opt-in non-sensitive media triage, not replacement of the faster existing text hot path. Family/project recommendations live in the owner's private complete directory inventory, not this public reusable repository. They are proposals, not live deployments. Real caller acceptance, held-out quality, privacy and accepted-correct cost must precede each promotion. No global provider switch, production app change, gateway restart or automatic spending is introduced.

## Sources and resource reuse

Official RightNow quickstart, public API/decisions, attachments, pricing and data-handling pages define the public contract. The legacy SKILL/SDK surface was inspected but not installed. Existing `jevkit.client` supplied strict validation and Jev baseline transport. Existing installer supplies root-only managed installation. Installed Co-Agent selector/phone/browser source inspection established concrete candidate-count and gate limitations. Reused operations resource guidance kept built/tested/installed/live acceptance separate. No private project map or API key is included here.
