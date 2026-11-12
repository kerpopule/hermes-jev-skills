# Protocol, privacy and skill-setting repair qualification

## What changed

The candidate reconciles released main 0.25.3 with the previously qualified client/trace/browser/search work, preserving contributor credit and release history. Version 0.25.4 is a development candidate, not a tag, release or public-main promotion.

- Routing applies a target only when the host supplies the active wire and its own resolver confirms the candidate's protocol. Unknown/custom capability and mismatches retain the complete original request; refusal invalidates the turn and suppresses stale model/effort notices. API-mode changes join the cached-request binding.
- Skill selection has settings > environment > unchanged 0.5/0.5 defaults. Hermes uses `plugins.entries.hermes-jev.settings.skill_selection`; the standalone CLI has explicit flags. Both standalone and merged selection use the same validated finite [0,1] gates. Invalid tuning is an explicit error/fail-open, never clamped or silently activated.
- Pre-search planning is local-only. A single offered plan is returned code-first; multiple plans are left with the caller. No remote-planning opt-in, new endpoint or larger disclosure is introduced. Ordinary post-search ranking is unchanged.
- Offline doctor separates the recorded routing state from privacy mode, reports standalone environment/default thresholds and explicitly leaves loaded-runtime state unverified. Existing install provenance separates checkout, published main and installed managed files.

## Actual checks

The final source passed 1,706 package tests and 71 routing-dashboard tests on local Python 3.9 and 3.11. Release/privacy text and diff checks passed. The text gate's existing 56 binary exclusions remain unscanned; no new binary asset is added here. Raw logs and first failures are preserved in the operator's private evidence, not published with machine paths.

`scripts/host_protocol_smoke.py` exercised the actual installed Hermes `PluginManager`, registration, request-middleware chain and model-derived protocol resolver in a disposable home with OS network denial. The real host reports OpenCode Go GLM/DeepSeek as Chat Completions and Grok as Responses. The candidate retained GLM when Grok was proposed, and allowed a compatible DeepSeek swap. The original request stayed unchanged. Removing the actual guard in an owned synthetic copy made this same probe fail at the incompatible-target assertion; canonical source passed afterward.

The dedicated regression suite additionally covers unknown active/candidate capability, resolver failure, protocol A/B/A cache invalidation, refusal-notice suppression, configuration/environment/default precedence, boolean/NaN/infinite/out-of-range input, actual threshold propagation and zero planner calls. Existing concurrency, explicit model/effort, manual-pin and Responses-shape assertions were retained. Their fixtures now declare synthetic compatible host metadata rather than assuming any provider namespace guarantees compatibility.

## First failures retained and repaired

Initial repair contracts failed as expected on missing tuning and the existing protocol/planning paths. The first broad run caught legacy fixture metadata and a bundled-picker compatibility assumption. A later run caught the version/changelog invariant. Critical self-review also reproduced a stale rejected-model notice path. Each was repaired and the same gates rerun; no assertion was weakened to get a pass. Self-review is not independent review.

## Boundaries

The host probe uses synthetic routing answers. It is not a live OpenCode transport call, live-model semantic-accuracy benchmark or proof that an already-running gateway reloaded. `evidence/repair-20261112/installed.json` records the root-only installation: all 162 managed files matched reviewed source, all 47 protected configuration/mode hashes stayed unchanged, and fresh installed CLI paths exercised one-plan/multiple-plan privacy behavior plus malformed-request and invalid-threshold refusal. A fresh interpreter imported the installed package and obtained an actual Jev answer selecting the relevant synthetic protocol document over an unrelated recipe; this is a functional smoke, not an accuracy benchmark. Live doctor reported reachable. The actual native host middleware/resolver probe passed again using the installed plugin under OS network denial. No new paid benchmark, credential/customer disclosure, mode promotion, generic custom-wire assertion, gateway restart, Pomerado activation, release or public-main merge is part of this candidate.

The stale-looking pools were reviewed against the owner model policy. Routing remains shadow and the owner model remains unchanged; cached-price inversions and duplicate specialty cells are not measured savings or a reason to activate another model. They are retained explicitly rather than silently rewriting calibrated preferences during a correctness repair.

Pomerado remains a separate optional/default-off PR. Its main reconciliation changed release/CI metadata and inherited the reviewed tagged-model fixes; the companion implementation was not silently enabled or installed as a server. Its own full Python/Node checks are qualified separately.

## Resource reuse

The existing hermetic middleware fixtures supplied model/effort/concurrency oracles. The actual host's model-switch/provider resolver supplied protocol truth instead of a second guessed model-name table. Existing privacy/planning validation and provenance code were retained rather than replaced. These source choices changed the implementation and executable acceptance, not just its documentation.
