# Stage 2 scorecard, 2026-09-22

Does skill selection's second request earn its ~500 ms, or would stage 1 alone do? Measured,
and the answer is that **stage 2 stays**. Method and the script that reproduces it:
`scripts/calibrate_skill_stage2.py`.

**What the two stages are.** Stage 1 cuts the catalog into batches of 120 and asks one Choice
per batch, side by side. Stage 2 is a second request: `needs_skill`, plus one Noul per
finalist, and the plugin only acts on what stage 2 leaves standing. After the merged request
(see `skills/jev-skill-select/SKILL.md`) stage 2 is the largest per-turn cost left, and it is
the last place where the answer can change for free — the two questions cost one request, not
two.

**Method.** 14 authored cases: 10 ordinary turns that should reach one named public skill, and
4 deliberately unremarkable turns that want no skill at all. Each case is run live **once** —
one merged request for stage 1, one verification request — and both policies are replayed from
the raw answers:

- **stage 1 only**: the highest-probability candidate above `SHORTLIST_FLOOR`, no verification
- **stage 1 + stage 2**: what ships

Two catalogs, because the answer differs by catalog size: this repo's own ten skills (a clone
reproduces it exactly) and a real fleet's 379 (run locally; the numbers are below, the skills
are not).

| arm | catalog | n | right | **wrong** | spurious | missed |
|---|---|---|---|---|---|---|
| stage 1 only | 10 skills | 14 | 10 | 0 | **4** | 0 |
| stage 1 + stage 2 | 10 skills | 14 | 14 | 0 | **0** | 0 |
| stage 1 only | 379 skills | 28 | 18 | **2** | **8** | 0 |
| stage 1 + stage 2 | 379 skills | 28 | 22 | **0** | 6 | 0 |

Per case, merged request + verification: median **428 ms** (10-skill catalog), **489 ms**
(379-skill catalog).

## What stage 2 actually buys

**On the small catalog, it buys precision and nothing else.** All ten real cases were right in
stage 1 as well — but stage 1 alone offered a skill on **all four** turns that want none
(`jev-skill-select` at 0.06 and 0.14, `jev-compaction` at 0.11, `jev-skill-select` at 0.12 —
every one of them above the 0.02 shortlist floor). Stage 2 withheld all four on `needs_skill`
0.17-0.45 against a 0.5 threshold.

**On the large catalog it catches confident mistakes**, which is the case that decides this:

```
click through the checkout flow in the browser and confirm each step
  stage 1 alone:  dogfood  (0.96, then 0.94 on the repeat)   ← wrong, and certain
  shipped:        jev-browser-use (needs_skill 0.85)         ← right
```

A stage-1-only policy would have sent the agent to `dogfood` for a browser task, twice out of
two runs, at 0.94-0.96 confidence. That is the failure mode a floor cannot catch: the floor
only sees *how sure* the answer is, and this one was sure.

## The honest caveats

**The expectations are one agent's judgement**, written by the same hands that run the eval, so
this measures agreement with a written intent rather than ground truth. The cases are
deliberately unambiguous to keep that cost small, and the whole list is in the script.

**The `None` labels only hold for the catalog they were written against.** On the 379-skill
catalog, "summarise the last three release notes" reached `release-notes` (0.78) and "rename
the column in the changelog table" reached `sql-queries` (0.68). Those skills do not exist in
this repo's ten, so both arms score them as spurious here — but on a fleet that ships them,
`release-notes` is a *correct* suggestion for the first. The fleet arm's spurious counts are
therefore an upper bound, and the signal to read from it is the two confident-wrong picks, not
the spurious column.

**One run per case on the small catalog.** The large-catalog arm ran twice. Jev's answers vary
run to run — the same turn answered `dogfood` at 0.96 and 0.94 — so treat single-run cells as
one sample, not a rate.

## Decision

**Stage 2 stays.** Dropping it would save roughly 400-500 ms on every turn that has a candidate,
and cost: a wrong procedure suggested with high confidence on a real turn (observed twice, on
the catalog sizes the fleet actually runs), and a skill offered on every turn that wants none.
Quality wins this trade at this price, and the price has already been cut twice today by the
merged request and the connection pool.

Recorded as a **negative result** on the alternative, the way `evals/choose-match/` records the
two-question gate that was measured and not shipped. What would reopen it: a stage-2 shape that
costs nothing extra (a question added to the merged request — impossible, the finalists are only
known after stage 1 answers) or a stage-1 confidence band that separates the confident-wrong
cases from the confident-right ones on a set bigger than 14.

## Reproduce

```bash
python3 scripts/calibrate_skill_stage2.py                       # this repo's 10 skills
python3 scripts/calibrate_skill_stage2.py --runs 2 \
    --root ~/.hermes/skills --root ~/.hermes/shared-skills --root ./skills   # a real catalog
```
