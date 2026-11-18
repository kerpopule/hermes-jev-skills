---
name: rightnow-evaluation
description: Use when qualifying RightNow against Jev decisions.
---

# RightNow evaluation, explicit opt-in

Use for a requested RightNow comparison, not as an automatic replacement for Jev.
Custom procedure inspired by RightNow's documentation, not a raw vendor-skill install.

1. Read current official `https://www.rightnowai.co/docs/decision-model/quickstart`, `pricing`, `data-handling` and `attachments`. The API-key-page `rp_` API is `POST https://api.rightnowai.co/v1/decisions`, model `rightnow-decision`. The earlier `rnsk_` API, SDK examples, boolean fields, uploads, sessions and streaming are DIFFERENT. Do not infer compatibility from a shared name.
2. Establish actual spending authority and an explicit finite budget. Keep keys in the caller's secret store/environment `RIGHTNOW_API_KEY`, never chat, source, logs, command arguments or skill files. If a key was posted in chat, recommend owner-side rotation; never repeat it. No purchasing, top-up or subscription changes. Customer data requires separate exact permission. Use synthetic/public inputs, not private screenshots, memory, mailbox or code by default.
3. In the managed repository run `python3 scripts/rightnow_eval.py` for local preflight. This makes zero provider calls and does NOT prove semantic performance. `jevkit.rightnow.ask` is an explicit library entrypoint, not a new global provider. The existing Jev provider resolution, tools, hooks, default model and thresholds remain untouched.
4. Only after approval, use `python3 scripts/rightnow_eval.py --live --authorized-budget-usd <approved-cap> --journal <new-local-jsonl>`. Caller supplies credentials privately through environment, never a CLI key option. Journal refuses overwrite and records reservations before each send. No retry or fallback, no Idempotency-Key retention. An unresolved charge stops the suite and retains its full reservation. This is a bounded smoke corpus, not a model-quality benchmark.
5. Preserve full descriptions: the adapter maps choice criteria to public `options` plus explicit option definitions, score criteria to `levels`, and preserves noul criteria in instructions. Conservative limits: 32 questions, 10 choices, 4 score levels. Box/coordinates and structured instructions are refused. Never silently truncate options or change rubric indices. Existing skills exceeding limits are NOT compatible without a separately evaluated decomposition.
6. Replies must pass the shared strict validator, exact answer IDs, normalized distributions, argmax, confidence consistency, score distribution and mean, finite cost and expected model identity. Partial refusal, abstention, missing cost, malformed response or a late result is unusable. Never synthesize probabilities. Confidence is not calibrated accuracy.
7. Compare matched same-input Jev and RightNow runs only within approved budgets. Measure accepted-correct decisions per dollar, false positives/negatives, abstentions, invalid replies, Brier score, p50/p95 wall latency including cold starts, and bounded failure behavior. Record exact model and API version, cache state, dataset labels and usage. The bundled corpus maps topics from all existing skill families; it does not execute all their production paths.
8. Promote one low-risk caller at a time ONLY after real caller-path and held-out quality acceptance. Exclude payment/send/delete/customer/security approval decisions. Keep deterministic safety and privacy filters local. A decision model proposes a label; it never grants authority or executes a tool.

## Question wording learned from live controls

Use direct positive questions with contrasting evidence for negative examples. Do not create the negative case by asking whether an entire interrogative is FALSE: both providers failed several such controls. Retain first-run failures and identify the revised cohort, rather than replacing evidence. Keep per-family caller acceptance separate from the tiny 13-family synthetic control corpus. The measured matched text run did not establish a RightNow cost or latency advantage; see the scorecard before proposing a text hot-path swap.

## Media

Load `rightnow-media-triage`. Files must first pass local preparation and human/agent visual or audio QA. The media transport surface is not evidence of perception quality. Do not send entire recordings, private camera streams or repositories by default.

## Evidence and gaps

Report built/tested/installed/live-qualified separately. Current adapter is evaluation-only. Do not say cheaper, better, calibrated, all-modalities qualified or fleet-switched without actual matched evidence. Preserve failed runs; don't retry until a lucky response passes. Store private project maps outside the public repository. Never save API keys, private payloads, customer identifiers or receipts containing them here.

Sources inspected: vendor SKILL.md and linked quickstart, API/decisions, inputs, pricing and data-handling pages. No vendor installer/SDK was executed; this adapter uses standard-library HTTP and existing strict validation. See `docs/rightnow-evaluation.md` for the full compatibility and acceptance matrix.
