# Frozen test-set collector

This standalone collector is opt-in, not a live plugin or routing change.
It verifies the exact preflight and source identities before inference. Provider
is TypeSafe and model is pinned to `jev-1.13.0`; threshold stays at 0.5.
Reserve identities are verified but reserve text is never screened or sent.

## Budget and outbound boundary

The live list price was checked at https://docs.typesafe.ai/models on 2026-10-08:
$0.042 per million input tokens; output is free. The published model context is
64k tokens. Before each HTTP POST, the collector reserves the list-price cost of
65,536 input tokens under a lock. It refunds only the difference confirmed by
valid provider-reported usage. Missing usage on a successful HTTP response retains
the worst-case reservation and stops new calls. A transport/HTTP failure retains
the full worst-case cost but allows other never-sent rows under the same cap.
In-flight requests remain reserved. This is conservative
list-price accounting, not an independently verified invoice.

The CLI accepts a positive USD cap no greater than $5. Actual invocation requires
explicit owner approval. A $5 cap is a limit, never an instruction to spend it.
No automatic retries, reconnects, redirects, threshold sweeps or provider/model
substitution. Model/usage/proxy/plan anomalies stop or refuse the run. The TLS
transport makes one POST to the exact TypeSafe API origin using headers supplied
by the existing client/key resolver; keys are never printed or recorded.

Only the existing redacted public-test projections go outbound. Receipts contain
hashes, row identities, usage, model IDs, timings, stable codes and actual applied
withholding verdicts, never input text, response bodies or authorization headers.
Files are 0600 in a 0700 directory. A new output directory is required. An explicit
`--prior` verifies the prior plan, outcomes, counts and reconstructed spend, then
continues only rows with no previous wire attempt. Every sent outcome, including
errors, stays unchanged; uncertain requests are never repeated. The total cap
carries prior spending and is not reset by continuation.

## Invocation

With explicit owner approval and the verified files staged privately:

```sh
python3 scripts/webscreen_collect.py --execute --usd-cap 5 \
  --test "$DATA/s-labs-test.csv" --reserve "$DATA/s-labs-validation.csv" \
  --plan "$DATA/unexecuted-preflight.json" --out "$DATA/authorized-run"
```

The rate is capped at 10 POSTs/second, with at most eight workers, a 540-second
collection deadline and four-second request timeouts. A provider error is not
retried. Failed and skipped rows remain explicit in denominators. An accounting
report marked complete only means every expected row has an outcome record;
`all_eligible_rows_ok` separately establishes successful model evaluation of all
sendable rows. Do not call a run with skipped/error rows a complete live replay.

## Self-review and offline verification

Seven targeted tests cover reservation/settlement, missing usage, the real client
validator, unavailable-result paths, pinned model, privacy exclusions,
proxy/plan refusal, one-POST no-retry/no-redirect behavior and safe continuation
that preserves sent failures and cumulative spend. All 1,437 tests pass on
Python 3.9 and 3.11 after that recovery change. The entire frozen corpus was
exercised once with an
explicit synthetic transport: 2,101 outcomes, 2,099 fixture requests and two
local-only rows. Those fixture values are not live detections and are never used
as evidence of model quality. Self-review is not independent review.

The collector is committed before any paid execution so a live receipt can pin
its exact revision. Fresh aggregate results, if collected, belong in a separate
sanitized scorecard. Full hash-only per-call/outcome receipts stay private.
