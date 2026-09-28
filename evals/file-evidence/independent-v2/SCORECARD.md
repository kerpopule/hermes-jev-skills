# Independent evidence validation v2: NOT_PROMOTED

## Decision

Keep deterministic-first and skip semantic scouting by default. Semantic triage remains explicitly approved, advisory-only Python functionality, not an automatic test repair or a root-cause verdict. Neither semantic mode met the frozen promotion bars. No thresholds, labels, policies or production modules changed after scoring. Stronger benchmark comparators are not silently promoted into production.

The baseline-first caller policy is justified: BM25 top-8 retained both critical references on all eight tasks; adding Jev retained exactly the same 60 references, while adding provider input, audit context and latency. The stronger deterministic failure comparator beat both semantic variants, including on actual executed Python failures. Batch composition reduced calls but did not rescue net benefit.

## Provenance and independence

Frozen before live scoring in commit `765061450092fdea64a9fa8d2f9dbdaf648c4fd4`, with exact hashes in `freeze.json`. There are 32 PSF-licensed CPython functions shared across eight two-critical-reference tasks; 64 failure records comprise 24 actual isolated Python executions and 40 explicitly authored multi-toolchain diagnostics. All labels, comparator signatures and thresholds preceded scoring. Prior A/B panels are development only and were not rerun. This is model-unseen validation, not independent human annotation or sampled production CI. V2 is now observed and must never be advertised as fresh for later tuning.

`summary.json` is the exact offline recount. `results/` preserves all nine shards, reservations, usage receipts, normalized readings, selected sets, excerpt text and full recoverable references. The full-source corpus is retained separately. All 318 exact outbound bodies were reconstructed offline and verified against original wire SHA-256 and byte length in `reconstructed-requests.jsonl`; reconstruction does not invent responses. Raw HTTP response bodies were not retained and cannot be recovered. Normalized answer distributions, model, token counts and timing survive. No credential or authorization header is included.

## Scouting

| Method | All-critical tasks /8 | Relevant selected / selected | Relevant recall /16 | Compact frontier estimate | Verbose estimate |
|---|---:|---:|---:|---:|---:|
| Exhaustive |8|16/256|16|52,805|52,805|
| Shipping lexical top8 |5|11/64|11|25,069|25,069|
| Shipping lexical top16 |6|13/128|13|35,067|35,067|
| BM25 top8 |8|16/60|16|24,390|24,390|
| BM25 top16 |8|16/94|16|29,067|29,067|
| Semantic only |1|6/7|6|16,605|16,605|
| Batch only |3|9/11|9|17,095|17,095|
| BM25 + single |8|16/60|16|29,118|71,844|
| BM25 + batch |8|16/60|16|29,130|40,280|

All 32 references recovered on each task. However, 233/256 recovered excerpts were shorter than their full functions. File-selection recall is NOT proof that the selected eight-line excerpt contains the needed answer. There were four path/URL-withheld observations; safeguards were not relaxed. Single advice abstained on 234/256 observations, batch on 209/256. Per-task missing critical IDs and all advice distributions are in the machine-readable summary and original rows.

Frontier estimates are ceil(UTF-8 serialized JSON bytes/4), not real frontier tokenizer usage or billing. They include query, complete eligible reference manifest, warnings and selected recovery text; enhanced compact envelopes also include id/action/reason summaries. Verbose envelopes retain full policy advice. Semantic-only diagnostic envelopes omit advice and therefore are optimistic, not deployable safety claims. Direct-reading all recovered excerpt bytes has an optimistic 20,160-token lower bound; all full-function bytes have a 67,992-token lower bound. Neither direct bound includes instructions, query or references.

Against BM25 top8, compact union adds 4,728 frontier tokens with single calls and 4,740 with batch. Including actual Jev input adds 122,913 and 64,286 net input tokens respectively. Against the same-envelope exhaustive baseline, union saves frontier context but still adds 94,498 and 35,871 net input tokens. Neither has positive combined-context benefit.

## Failure triage

| Method | Correct /64 | Actual executions /24 | Authored /40 | Correct accepted / accepted | Abstentions | Unknown correctly withheld /22 |
|---|---:|---:|---:|---:|---:|---:|
| Shipping deterministic |41|22|19|19/19|45|22|
| Strong deterministic |62|22|40|40/40|24|22|
| Shipping + Jev |60|20|40|40/42|22|20|
| Strong + reused Jev |60|20|40|40/42|22|20|

There are 42 known and 22 unknown gold records. Both semantic variants wrongly asserted syntax on `executed-19` and `executed-21`, whose gold is unknown. They also abstained on dependency cases `executed-05` and `executed-07`. Original exit codes were preserved for all 64 cases. No response claimed proven root cause. Strong+Jev reuses the same advice and has no independently measured latency. Improvements over the weak baseline came from authored examples, not the actual Python execution subset. This is why the 60/64 headline alone is misleading.

## Usage, latency and separate monetary tradeoff

| Mode | Actual requests | Provider input tokens | List-price estimate USD | HTTP p50 / p95 ms |
|---|---:|---:|---:|---:|
| Single scouting |252|118,185|0.00496377|165.79 / 276.98|
| Batch scouting |32|59,546|0.00250093|345.63 / 502.10|
| Triage |34|17,210|0.00072279|165.18 / 334.90|
| Total |318|194,941|0.00818749|172.30 / 361.48|

All 318 reservations have responses and reported usage; missing usage is zero. Prices are repository list estimates, not invoices. No frontier provider was invoked. HTTP percentiles use nearest-rank; aggregate n=318. Per-task single-union p50/p95 is 5,993.92/7,242.03ms; batch-union 1,725.37/1,983.84ms (n=8 each). Local scout p50/p95 is193.69/283.87ms plus BM25 4.67/10.20ms. Baseline triage p50/p95 is0.67/2.78ms; extended0.74/3.14ms; enhanced138.07/288.35ms (n=64). Sequential single-then-batch order is not randomized crossover, so timings are descriptive.

A positive dollar crossover can coexist with negative total tokens when Jev input is cheaper. Against exhaustive compact delivery, single union breaks even at approximately $0.209557 per million avoided frontier input tokens; batch at $0.105636. Against stronger BM25 top8 there is no positive-price crossover because enhanced delivery is larger even before paying Jev. This is not an argument to use weak exhaustive delivery as the practical baseline. Accuracy/safety bars still fail regardless of a hypothetical token price.

## Reproduction and rollout boundaries

Run offline from the review checkout:

```
python3 scripts/reconstruct_file_evidence_requests.py
python3 scripts/summarize_file_evidence_independent.py
python3 -m unittest discover -s tests
python3 scripts/check_release.py
```

These commands do not repeat live model scoring. The reconstruction intercepts before credential resolution, limiter access and transport. Benchmark request order, frozen corpus and policy hashes remain unchanged. No batch production API or automatic caller hook was added.

Supported callers and candidate/canonical installation skew are documented in `docs/file-evidence-integration-audit.md`. Current execution instructions explicitly bar source skill edits; matching source skill integration is blocked, not complete. No installed skills were edited. No main merge, CLI retarget, shared install, gateway restart, routing change or production promotion is authorized by this evaluation. A deliberate source-skill and canonical-CLI rollout remains a separate gate.
