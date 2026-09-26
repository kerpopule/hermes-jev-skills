# From shadow to live: decision policies

Read [turning-a-jev-feature-on.md](turning-a-jev-feature-on.md) first. This page is the working part: how a small agent decision becomes a Jev decision, how you measure it on your own history, and what has to be true before it changes anything.

## What a policy is

"LLMs think, Jev decides, tools act." A **policy** is one small decision written as a JSON file in `jevkit/policies/`:

- **Questions** for Jev about one state: yes/no (noul), pick one (choice), or a position on a scale (score). All of a policy's questions go in **one request**.
- **Pre-rules** that code checks first, using facts your own code worked out ("the monitor's source failed", "first run"). When one matches, Jev is not asked. Nothing is sent and nothing is paid.
- **Rules** that turn Jev's numbers into an action. The first rule that matches wins; `otherwise` is used when none does.
- **`on_error`**: what happens when Jev cannot answer. It is always what the agent did before this existed.
- **`promotion`**: the bar a shadow run must clear. It is written down before any data is seen.

Every decision says where it came from: `source` is `code`, `jev` or `fallback`.

| Policy | Decides | Today's behaviour (the fallback) |
|---|---|---|
| `gate-strict`, `gate-permissive` | approve / ask a person / deny a flagged command | the host's own approval flow (`no_opinion`) |
| `cron-wake` | wake the agent for a monitor tick, or skip | wake |
| `retry` | retry a failed worker, retry with more budget, or hold for a person | retry |
| `blockcheck` | does a blocked card really need the owner? | needs the owner |
| `owner` | which worker profile should take a card | the creator's pick |
| `kanban-event`, `triage-urgency`, `evaluator-default` | event routing, urgency, output grading | the caller's default |

`jev policies` lists them. `jev policies show NAME` prints one. A local override goes in `<hermes root>/jev/policies/NAME.json` and is logged under its own hash.

## Everything is off

Nothing here runs because it is installed. Each feature has a mode (`jev switches`, or `/jev <feature> <mode>` in Hermes) and a kill-switch file, `<hermes root>/jev/<FEATURE>_OFF`, which always wins. The plugin's gate hooks and decision tools load only when the gateway starts with them switched on.

## The loop

**1. Build rows from your own history.** One JSON object per line:

```json
{"id": "run3368", "state": {"outcome": "timed_out", "error_tail": "..."}, "facts": {"budget_exhausted": true}, "truth": "failed_again"}
```

`state` is what Jev would see. `facts` are what your code knows; they are never sent. Every other field is a label, copied to the output.

**2. Run the policy over them.**

```bash
jev batch --policy retry --in rows.jsonl --out decisions.jsonl          # prints the estimate, sends nothing
jev batch --policy retry --in rows.jsonl --out decisions.jsonl --yes    # the paid run; resumes if stopped
jev batch --policy retry --in rows.jsonl --out jev-only.jsonl --yes --jev-only   # same rows, pre-rules skipped
```

The batch goes through the same `decide` function a live feature uses, with the same redaction. A 429 that names a wait is waited out. The daily spend cap applies.

**3. Read the report.**

```bash
jev shadow report --feature retry --rows decisions.jsonl --policy retry [--by profile]
```

The report gives:

- a confusion table against the truth (and against today's mechanism, when rows carry `current`);
- the one error that matters most for the feature, with a count and example ids;
- every promotion criterion, marked `PASS`, `FAIL` or `UNKNOWN`. `UNKNOWN` means the rows cannot judge it. It never counts as a pass.
- the **calibration** of the probability the feature relies on:
  - the **AUC**: 0.5 means the number says nothing about the outcome, and no threshold can fix that;
  - a **reliability table**: when Jev says 0.8, how often was it right;
  - a **threshold sweep**: what each cut-off would have flagged, and how often rightly.

`sources` counts how many decisions code made without asking Jev.

**4. Tune, for free.** Copy the policy to a local override and change a threshold or a pre-rule. Then re-run the batch with `--rescore`, which applies the new rules to the answers already recorded. Nothing is sent. Changing a question's wording is different: the old answers were to the old question, so it needs a new paid run.

Pick thresholds from one set of rows, then judge them on another. A threshold fitted to the same rows it is judged on will look better than it is.

**5. Live shadow**, then **live**, and only on a `PASS`. In shadow the decision is logged and nothing changes. The switch that makes it act is a person's.

## What each feature compares against

| Feature | `truth` | Useful extra labels |
|---|---|---|
| `gate` | `approve` / `deny`: the person's own choice (timeouts left out) | `expect` (`must_deny` / `must_ask` / `should_approve`) for red-team rows; `current` |
| `cron_wake` | `report` / `silent`: what the agent run actually said | `report_kind` (`failure` / `decision` / `other`); `baseline` (`wake` / `skip`) for the hash-only rule |
| `retry` | `completed` / `failed_again`: the next run's outcome | `baseline_hold` (true/false) for the same-error rule; `ts` |
| `blockcheck` | `needed_owner` / `not_owner`: how the block was really cleared | `baseline_not_owner` for the current code rule |
| `owner` | the profile that completed the card | `creator`: the creator's pick |

## The command gate on a red-team set

`evals/gate/fixtures.jsonl` holds 118 made-up commands:

- 35 that must be denied;
- 30 that must go to a person;
- 53 ordinary ones that a pattern list flags anyway.

```bash
jev gate replay evals/gate/fixtures.jsonl --policy gate-strict --out /tmp/gate.jsonl --report --yes
```

The number that matters is **false approves**: a command that had to be caught, approved. The target is none. The second number is friction: harmless commands that were not approved. Jev scoring your *own* approvals is the same replay on rows built from your approval log. The plugin's shadow hooks write `logs/jev-gate.jsonl` as ids, hashes and probabilities only; they never write the command.

## Ways to make a decision cheaper or better, all measurable with the loop above

- **Code first.** A pre-rule for anything a fact already settles. Keep a pre-rule only if the rows say code gets that case right. Two that looked obvious failed on real history. "Output unchanged, so skip" was wrong on all 6 ticks it fired. "Same error twice, so hold" was wrong on half of its holds. Both are written up in the policies' `code_first` notes.
- **Ask everything at once.** All of a policy's questions go in one request. More questions cost a few tokens, not another round trip.
- **Wording.** One sentence on what a yes and a no each mean (noul `criteria`) moves answers measurably. See [writing-a-jev-question.md](writing-a-jev-question.md).
- **Score, then choose.** Ask a score and a choice together, and let a rule read both (`urgency.norm >= 0.75` and `cause.choice == ...`).
- **Jev when confident, the model when not.** A noul between 0.30 and 0.70 is `unsure`, and so is a choice with a small `margin`. Send those to the caller's own model or a person. Rules can name `q.unsure` and `q.margin` directly.
