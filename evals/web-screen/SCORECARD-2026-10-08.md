# Frozen public screening replay: 2026-10-08

## Verdict

Keep issue #25 open. On the frozen public S-Labs regression set, the existing
Jev-plus-local screening path withheld **322/1,051 attack-labeled rows (30.64%)**
and **1/1,050 clean-labeled rows (0.095%)**. This is not adequate evidence for a
standalone security boundary. No detector, question, threshold or default was
changed, and no reserve inference or threshold sweep was performed.

These are dataset-label withholding rates, not adjudicated web-injection recall
or downstream agent safety. The public test includes examples previously
reported by the issue author; it is not a fresh hidden holdout. The earlier
40-row external result and this 2,101-row replay differ in population and input
availability, so these numbers do not establish a temporal model regression.

## Actual execution and denominator accounting

- Frozen manifest SHA-256:
  `ccdb9061dc195bb7f620025dbf19752bb855ae4f027624c29e01f5d1c44e103b`.
- 2,101 expected test outcomes, all recorded; no missing or skipped rows in the
  final combined receipt. 1,985 reserve identities verified, zero reserve calls.
- 2,099 unique wire requests, each attempted once. 2,088 valid replies; 11
  fail-open outcomes and two sensitive/local-only clean-labeled rows remain in
  group denominators. No uncertain request was retried.
- Attack group: 1,045 valid replies, six fail-opens; all 322 withheld rows had
  valid replies. Clean group: 1,043 valid replies, five fail-opens, two local-only
  rows; its one withheld row had a valid reply.
- Errors: five HTTP 529 responses, five network errors and one initial HTTP error
  whose exact status was not captured. No status is retrospectively inferred.
- Provider `typesafe`, requested and observed model `jev-1.13.0`, threshold 0.5.
  Template hash `a89b1bed2777ad54744a8a70ec40e0e5e140dcb76671661497ab113d76e4e8a7`.
- Collector revisions `38395b553ddfa1714d571acef4afc2d2ccba961c` and
  `27b782507582665ccc67b355143c6508e35c6b3d`. The original run conservatively
  stopped after one usage-unknown HTTP error. A tested continuation verified the
  prior source/plan/outcomes/spend, retained every sent outcome, and processed
  only never-sent rows under the same total budget. Detector file hashes stayed
  identical. Combined collection time: 342.219 seconds, excluding development.

Wilson 95% intervals from the existing accounting harness are 27.93–33.49% for
attack-labeled withholding and 0.017–0.537% for clean-labeled withholding. These
simple intervals do not establish dataset-label correctness or generalization.

## Cost and approval

Steve authorized up to **$5** for this replay. The current official list price
was independently fetched from https://docs.typesafe.ai/models: $0.042 per million
input tokens, output free. The collector reserved the documented context-size
upper bound before each single POST, with no transport/client retries, and
carried prior spend through continuation rather than resetting the cap.

747,146 known provider-reported input tokens price to **$0.031380132**. Eleven
usage-unknown attempts retain their full worst-case reservation, making the
conservative combined upper bound **$0.061657764**, below $5. This is known-usage
list-price accounting plus an uncertainty allowance, **not invoice verification**.
No additional spending is needed to make this finding, and the unused approval
is not a reason to tune on or repeatedly replay the test set.

## Verification and private receipts

`LIVE-2026-10-08.json` contains the sanitized actual report, status breakdown,
source/provenance hashes and spending accounting. Full per-call wire hashes,
usage, timings, fixed codes and outcome receipts remain private. No raw attacks,
clean text, authorization headers, credentials or response bodies are published.
Combined receipt SHA-256:
`3428a3e1ce30dbec749e569a27f9417a644c80f165a7faac146b376a90b3a313`.

The collector's seven targeted offline tests include source/proxy refusal,
reservation/settlement, real-client validation, privacy boundaries,
no-retry/no-redirect POSTs and continuation that cannot replay an uncertain sent
request or reset prior spend. All 1,437 tests pass on Python 3.9 and 3.11 after
recovery. This
is critical self-review and QA, not independent review or a guarantee of safety.

## Next policy

Retain the explicit limits in the README and #25. Treat screening as an advisory
layer, combined with tool permission boundaries and trusted instruction
separation; do not present its structured output or confidence as proof that
retrieved content is safe. Any future detector/question change needs a separate
proposal/development set and preregistered confirmation. Leave the reserve
untouched until that distinct experiment is authorized.
