# Permutation averaging scorecard, 2026-09-22

Should `choose` and skillpick's stage 1 ask their Choice questions in several option orders
and average the vectors — `TypeLLM/pijev`'s whole trick? Measured on both surfaces against
this repo's own labelled corpora, and **nothing was shipped**: at the production floor the
decision set is identical, and on the one case in the decision band, averaging plus pijev's
confidence swap turns a reliably declined trap into an acting wrong answer.

Method and the scripts that reproduce it: `scripts/calibrate_choose_permutations.py` (31
cases, one solo + one batch of up to 9 orders each, 62 calls, 0 failures, plus a focused
trap repeat of 3 x (solo + batch), 6 calls) and `scripts/calibrate_skillpick_permutations.py`
(14 cases, 459-skill fleet catalog, production request + 3-order request each, 28 calls,
0 failures). Raw answers: `/tmp/perm-avg-choose-raw.json`, `/tmp/perm-avg-trap-repeat.json`,
`/tmp/perm-avg-skillpick-raw.json`. Live model `jev-1.13.0` via `jev-latest`. Total spend
under $0.05.

**The bars, stated before the numbers were seen** — from the scripts' own docstrings:

> `choose`: averaging is adopted only if, at the 0.65 floor, it matches or beats solo on
> WRONG ACTIONS (solo's record is zero per run) AND strictly beats it on stalls or on Brier
> loss for the right label.

> skillpick: averaging is adopted for stage 1 only if it produces ZERO wrong and STRICTLY
> fewer spurious stage-1 verdicts than canonical, with no more misses.

## `choose`: the floor makes the decision set identical, and the one open case goes wrong

31 cases, headline at the shipped 0.65 floor (`right/stalled/declined/wrong`):

| arm | Brier (24 labelled) | at 0.65 | at 0.50 |
|---|---|---|---|
| A solo (production today) | 0.024 | 23/1/7/0 | 23/1/7/0 |
| A' batched canonical (confound check) | 0.017 | 23/1/7/0 | 23/1/7/0 |
| B2 averaged (2 orders, pijev semantics) | 0.020 | 23/1/7/0 | 24/0/7/0 |
| B3 / B5 / B9 averaged | 0.023 / 0.025 / 0.026 | 23/1/7/0 | **1 wrong action** |
| C2-C9 decision-only (native confidence) | same as B | 23/1/7/0 | 23/1/7/0 |

- **Order barely moves the pick.** 21 of 24 labelled cases had one distinct top across all
  nine orders; two showed p(right) spread above 0.05 (`menu-find` 0.86-0.97, same top
  throughout). The only top-flip between acting ids in 31 cases was the known trap.
- **The Brier movement is one case.** Excluding the trap every arm sits at 0.0002-0.0006;
  B2's apparent win (0.020 vs 0.024) is the trap alone and reverses by m=3 (B9 is 0.026 —
  worse than doing nothing).
- **The batch confound is real but tiny.** The canonical question inside a 9-question
  request matched solo's decision in 30 of 31 cases (the trap flipped right in run 1).

## The trap: averaging amplifies a majority that is wrong, and the confidence swap makes it act

"Cancel this dialog without losing my work" — the corpus's one historically wrong answer
(`choose._floor()`: wrong 9 runs in 10 but never above 0.58). Right action `btn-cancel`;
the model leans `btn-save`.

- First run, nine orders: `btn-cancel` won **2 of 9**. The B2 "fix" flipped the trap right
  (24/0/7/0 at a 0.50 floor) — and it was pair luck: the canonical order and `__p1` were
  exactly the two lenient draws. Every aggregation from m=3 up (mean, median, trimmed mean)
  picked `btn-save`.
- `--trap-repeat`, three fresh runs of nine orders: wrong answer won **9-0, 8-1, 9-0**.
  The 2-of-9 split does not reproduce.
- pijev's confidence is the winner's **mean probability**, not Jev's own. On the repeat runs
  that put the wrong `btn-save` at **0.698 and 0.740** — through the shipped 0.65 floor —
  where solo's native confidence (0.52, 0.52, 0.43) declined it every time. At a 0.50 floor
  B3/B5/B9 already showed one wrong action in the first run. This fails the bar's wrong-
  actions leg decisively.
- **Agreement is not honesty.** The wrong answer came back unanimous twice in three repeats,
  so a unanimity or order-spread safety flag would have waved it through. Any aggregation of
  the order sample is an aggregation of the model's votes; when the votes are majority wrong,
  nothing in the sample distinguishes a unanimously wrong answer from a right one. This is
  the same wall the stakes/margin scorecard found: confidence does not separate right from
  wrong on this case, and order samples do not restore the separation.

## skillpick stage 1: identical verdicts, and one lone shuffle beat both arms

14 authored cases over the 459-skill fleet catalog (4 batches x 121 options), stage-1 top-1
verdicts counted exactly as `calibrate_skill_stage2.verdict` counts them:

| arm | right | wrong | spurious | missed |
|---|---|---|---|---|
| A canonical (production) | 10 | 0 | 4 | 0 |
| B averaged (canonical + 2 shuffles) | 10 | 0 | 4 | 0 |
| B' shuffles only / canonical + 1 shuffle (either) | 10 | 0 | 4 | 0 |
| rnd shuffle 0 | 10 | 0 | 4 | 0 |
| rnd shuffle 1 | **11** | 0 | **3** | 0 |

- No averaging variant moved a single verdict; the four stage-1 spurious picks
  (`sql-queries` for "rename the column in the changelog table", `release-notes`, etc.) are
  order-robust **semantic near-matches** — they are stage 2's `needs_skill` gate's job, not
  order noise (consistent with the stage-2 scorecard: spurious 8/28 before verification).
- One single random order out of two beat canonical AND averaging on spurious (3 vs 4) —
  sampling noise, not signal, and averaging averaged the gain away.
- Rankings were order-stable where it counts: all 10 expected skills held one distinct
  in-batch top across the three orders (p spread at most 0.09); top-5 finalist overlap
  between arms was 5/5 in five sampled cases, 4/5 once.
- Cost of the treatment: the 3x-question request took median 631 ms vs 278 ms.

Bar failed on both legs of the comparison (spurious 4 is not fewer than 4).

## Verdict: NOT adopted. Nothing in `jevkit` changes.

`choose` keeps its native `confidence` and the 0.65 floor; skillpick's stage 1 keeps
discovery order. The pijev package itself remains reference-only (day-zero third-party,
confidence semantics silently changed — audit in the appendix below).

**Why condition 2 was not priced separately (2026-09-22, decided on the evidence in hand):**
a disagreement-shrinking confidence has nothing to shrink on — the trap's wrong answer was
unanimous 9-0 in two of three repeats — and any magnitude-based threshold is a recalibrated
floor under new semantics: mean-probability confidence with a floor anywhere in 0.55-0.90
reproduces today's 23/1/7/0 exactly (the table above), i.e. today's behavior at 3x the
questions. No live calls needed to close it.

**What would reopen this:**

1. A corpus where the decision band is non-empty **and** single orders are individually
   unreliable but the majority is right. Averaging is variance reduction on a majority vote:
   it is safe exactly when the majority is honest, and our one torn case is majority-wrong,
   so it amplified the failure. `scripts/calibrate_choose_permutations.py` re-runs for $0.01.
2. Confidence semantics that **shrink** under disagreement instead of inflating (e.g. mean
   probability x agreement). Dead on this data by the unanimity finding above — the repeats
   were unanimously wrong — but worth pricing against a corpus with a wider torn band.
3. If pijev-shaped replies (mean-probability confidence) ever enter the fleet another way,
   `choose.MIN_CONFIDENCE` must be recalibrated — the number is calibrated on Jev's native
   confidence and means something else under the swap.

`tests/test_permutation_averaging_eval.py` pins the replay math and the measured failure on
the recorded trap numbers (0.698, 0.740, the 2-of-9 pair-luck flip), so the story survives
even when /tmp does not.

## Appendix: the `TypeLLM/pijev` audit that started this (2026-09-22)

- **What it is:** `github.com/TypeLLM/pijev`, "Permutation Invariant Jev" — ~178 lines
  wrapping `typesafe-sdk`, expanding each Choice into sampled option-order permutations in
  one `system_one` request and averaging the vectors. PyPI `pijev` 0.1.0, Apache-2.0,
  created 2026-09-22 (day zero at audit time), 19 stars.
- **Verified:** license consistent (GitHub field = root LICENSE = README); no installer, no
  `curl | bash`, no global writes, no Hermes/profile/cron paths; examples read
  `TYPESAFE_API_KEY` from env or gitignored `.env` and never print it; the odd
  `import httpx2` is legitimate (`typesafe-sdk` itself depends on `httpx2>=2.0.0`); README
  claims match the code, and its own experiment page admits its 20-point swing does not
  isolate option order.
- **Risks:** day-zero third-party pinning `typesafe-sdk>=0.7.1,<0.8` while importing SDK
  surfaces loosely; `confidence` silently becomes the winner's mean probability (this is
  the swap priced above); per-Choice `Random(seed)` re-seeding correlates permutation
  samples across same-sized Choices.
- **Recommendation (then and now): reference-only, do not install.** Anything that changes
  Jev behavior in this fleet lives in this repo with these tests — the trick was worth
  measuring, and measuring it answered the question without adopting the package.
