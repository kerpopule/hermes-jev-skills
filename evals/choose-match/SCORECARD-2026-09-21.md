# Two-question gate scorecard, 2026-09-21

Does gating `choose` on a **second** question buy fewer wrong actions? Measured, and the
answer is no — so the gate did not change. Method and the script that reproduces this:
`scripts/calibrate_choose_match.py`.

**What prompted it.** Stagehand's Act primitive accepts a Jev pick only after asking two
questions — *which candidate is best*, and *does any candidate match the goal at all* —
at a 0.7 threshold, falling back to a language model otherwise. This repo gates on one
number, `choose.MIN_CONFIDENCE`. The two failures being separated are real and different:
"the best candidate is weak" versus "the right action is not on this screen", and one
probability cannot tell them apart.

**Method.** The 31 labelled cases `scripts/calibrate_choose.py` already carries (16
`answerable`, 8 `trap`, 7 `no_answer`), with on-screen regions, live against
`api.typesafe.ai`, Jev `jev-latest`. One call per case with **both questions in the same
request**, raw answers kept, every gate replayed offline. Five runs of the same cases
(one ran 30, after a single call timed out). The second question:

> Is one of the candidate actions, other than `reobserve` and `abstain`, the correct next
> action for the goal — advancing it without a wrong, irreversible or destructive effect?

The valves are named because they are always on the table and always "match" in the weak
sense; a question that accepts them is answered yes on every screen ever observed.

## The second question does carry a real signal

Straight from the live answers, over three runs of the same 31 cases:

| kind | n | match min | median | max | mean confidence |
|---|---|---|---|---|---|
| `answerable` | 16 | 0.61-0.75 | 0.93-0.94 | 0.96 | 0.99 |
| `trap` | 8 | 0.75-0.80 | 0.95-0.96 | 0.96-0.97 | 0.92-0.95 |
| `no_answer` | 7 | 0.10-0.11 | 0.15-0.17 | **0.43-0.45** | 0.47-0.50 |

The `no_answer` band tops out at 0.45 in every one of the five runs and everything else
starts at 0.61, so "nothing here serves the goal" is separable on this set. Confidence
alone separates it less cleanly: two `no_answer` cases answered at 0.58-0.60 and 0.70,
at or above the floor's own noise line.

## It buys nothing for the decision the gate makes

Gates replayed on the raw answers, ranges across the five runs. `-` is the single-question
floor this repo ships.

| floor | match | right | stalled | declined ok | WRONG ACTIONS |
|---|---|---|---|---|---|
| 0.60 | **-** | 23 | 0-1 | 7 | **0-1** |
| 0.65 | **-** (current) | 22-23 | 1 | 7 | **0** |
| 0.70 | **-** | 22-23 | 1 | 7 | **0** |
| 0.60-0.70 | 0.50 | same as the floor row | | | 0 |
| 0.60-0.70 | 0.60 | same as the floor row | | | 0 |
| 0.60-0.70 | 0.70 | 22-23 | 1-2 | 7 | 0 |
| 0.60-0.70 | 0.80 | 19-21 | 2-4 | 7 | 0 |
| 0.60-0.70 | 0.90 | 17-18 | 6 | 7 | 0 |

The same rows with the second question as the *only* gate, confidence ignored:

| match only | right | stalled | declined ok | WRONG ACTIONS |
|---|---|---|---|---|
| 0.50 | 22-23 | 0 | 7 | **1** |
| 0.60 | 22-23 | 0 | 7 | **1** |
| 0.70 | 22-23 | 0-1 | 7 | **1** |
| 0.80 | 19-21 | 2-4 | 7 | 0-1 |
| 0.90 | 17-18 | 6 | 7 | 0 |

## What that says

1. **The floor already covers the case the second question would catch.** At 0.65 the
   single-question gate produced **zero wrong actions in all five runs** — the one known
   wrong answer (`Cancel this dialog without losing my work` → picks *Save*) came back at
   confidence 0.45-0.60 across them, under the floor every time. At 0.60 it slipped through
   once in five, which is the margin `_floor()` describes and the reason 0.65 is the floor.
2. **Where the second question is used alone it is strictly worse.** That same trap case
   answers `match` 0.79-0.83, so a match-only gate at any useful threshold acts on it: 1
   wrong action in **every** run, against the floor's 0. It recovers the one stall the floor
   takes and pays for it with the one wrong click, which is the trade this repo's metric
   exists to refuse.
3. **Loosening it to where it changes nothing is not worth a request.** At 0.50-0.60 the
   added gate reproduces the floor row exactly, in every run. Above that it only converts
   right answers into stalls (0.80: two to four of them).
4. **So the second question was not adopted.** Nothing about `choose` changed: same one
   question, same floor, same `reobserve` fail-open, and no extra tokens on a call this
   repo already makes. The finding is recorded here and in `jevkit/choose.py` so it does
   not get re-litigated from the same premise.

## Reading it honestly

- **31 cases, and the floor was calibrated on them.** That is the real limit: `0.65` was
  chosen against these very rows (`_floor()`'s own note), so this comparison is measured
  on the floor's home turf. It can show that a second question adds nothing *here*; it
  cannot show the floor does not overfit them. A held-out set of screens is what would.
- **Zero wrong actions leaves the second question no room to help.** Absence of an
  improvement is a weaker claim than a demonstrated harm, and the harm only shows up in
  the match-only rows. A second question can only earn its place where the floor is
  demonstrably letting wrong answers through, which is not what these runs show.
- **Run-to-run noise is small on this decision, unlike the floor's own history.** Two runs
  compared case by case, 30 cases each: 30/30 identical picks, no case changed verdict, mean
  absolute move 0.011 in confidence and 0.009 in `match`, worst 0.09 and 0.07.
- **One call timed out** in one run (`Jev unavailable (timeout)`) and was excluded rather
  than scored. The fail-open path in `choose` returns `reobserve` there; a gate that is only
  correct when the network cooperates is not a gate.