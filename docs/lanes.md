# Lanes: the smallest model that still gets it right

After @0x_rody's "Claude → Opus 5.5 → Jev: multi-model coding orchestration" and "Top 10 Jev Builds You Can Ship in an Afternoon". The pattern:

1. **An orchestrator, not a worker.** Finish each piece of work with the smallest model and the lowest effort that still gets it right.
2. **Jev decides, it does not do.** Jev classifies complexity, routes effort, and decides continue/stop, retry, escalation and completion. It never writes code, patches or designs, and it is never asked what a tool can answer.
3. **Four lanes**, lowest sufficient first: `small`, `medium`, `high`, `escalate`.
4. **Deterministic checks first**: test runner, compiler, type checker, linter, `git diff`.
5. **After each cycle**: inspect the diff, run the checks, gather short evidence, ask Jev only what is left, then continue / retry / verify / escalate / complete.
6. **Escalate one lane at a time, on evidence only**: repeated failures, failing checks, security-sensitive changes, unresolved design doubt, low Jev confidence.
7. **Complete only** when the behaviour is implemented, the checks pass, the diff matches the scope and nothing is unresolved. Never hide a failed check.

## The pieces

| Piece | What it does |
|---|---|
| `jevkit/policies/lane.json` | First lane. One request: `lane` (a choice with an `other` escape), `security_sensitive`, `underspecified`. Pre-rules: a model the person named, two failed attempts, a security path. |
| `jevkit/policies/lane-kanban.json` | The same questions with thresholds for agent-sized cards (a Kanban task is a whole job, not one edit). Recorded answers rescore between the two for free. |
| `jevkit/policies/loop-step.json` | After each cycle. Pre-rules on deterministic facts decide failing checks, scope, empty diffs, unrun checks and security changes; Jev judges `implemented`, `in_scope` and `next`. |
| `jevkit/lanes.py` | Lane → model/effort per host, `evidence()` (runs the checks, reads `git diff`, keeps only the tail of long output), `classify()`, `step()` (refuses a `complete` the facts do not support). |
| `jev lane …` | `classify`, `step`, `evidence`, `next`, `targets`, `replay-build`, `replay-report`, `shadow`, `shadow-report`. |
| `claude/agents/jev-lane-*.md` | Claude Code subagents whose frontmatter sets `model` and `effort` per lane. |
| `claude/claude-md-block.md` | A short, delimited block the installer adds to `~/.claude/CLAUDE.md` (backed up first; `--no-claude-md` skips it; `--uninstall` removes exactly the block). |
| `jevkit/lane_shadow.py` | Hermes: a cron tick that classifies new Kanban cards in shadow, and in `on` sets the card's model and effort. Switch `lanes`, kill file `LANES_OFF`. |
| `jevkit/policies/gate-task.json` | The command gate with the task: a call that does not fit the task, or writes somewhere other people see, asks a person; destructive over 0.5 with serious severity stops. `jev gate check --task …`. |
| `evals/lanes/replay_git.py` | A controlled replay: past commits redone by Claude Code, lanes against always-top, success checked by the commit's own tests. |

## Claude Code

The subagent's model and effort come from its definition's frontmatter (`model: haiku|sonnet|opus`, `effort: low|medium|high|xhigh|max`); an explicit `model` on the Agent call overrides it. So a lane is a subagent type:

| Lane | Subagent | Model | Effort |
|---|---|---|---|
| small | `jev-lane-small` | Haiku 4.5 | low |
| medium | `jev-lane-medium` | Sonnet 5 | medium |
| high | `jev-lane-high` | Opus 5.5 | medium |
| escalate | `jev-lane-escalate` | Opus 5.5 | high |

`jev lane classify` names the subagent. After it returns, `jev lane step --run "<tests>" --scope "<paths>"` decides the next move.

## Hermes

A Kanban card carries `model_override`, `provider_override` and `reasoning_effort`, and the dispatcher passes them to the worker. So a lane is two fields on the card, set before dispatch. The default map was calibrated on one fleet's history (below): `small` = `gpt-5.6-luna` at medium, `medium` and `high` = `gpt-6-sol` at medium (today's default), `escalate` = `gpt-6-astra` at high. Override any field in `<hermes root>/jev/lanes.json`.

```bash
jev lane shadow                 # from cron every few minutes; does nothing until /jev lanes shadow
jev lane shadow-report          # the shadow log joined to how those cards actually ran
```

## What one fleet's history says

Measured 2026-09-26 on real history, read-only. Tokens are input + output (cache reads excluded); "first try" means the card's first run completed.

**Hermes Kanban, 2,357 cards** (private profiles excluded), almost all run on Sol at medium effort:

- The article's lane thresholds put 64% of cards in `high` (Jev's median confidence on a whole card was 0.42). Mapped the article's way (high = more effort), that is **+52% tokens** against running everything on today's default, and -15% against always-top.
- Jev's difficulty still carries signal: first-try success falls from 86.6% to 75.9% across difficulty quintiles (AUC 0.58 for predicting a first-try failure), and cards it marks security-sensitive (≥ 0.7, n = 134) finished first try only 66.4% of the time.
- Effort, measured within the same profile: high effort cost **1.79×** the tokens of medium (4 profiles), with no first-try gain, even on cards Jev called hard (73.2%, n = 41, against 74.8%, n = 302 at medium). Low effort cost **2.14×** (2 profiles, 36 cards): more turns, not fewer.
- A smaller model did not lose: on the profiles that ran both, `gpt-5.6-luna` used 0.49× the tokens of `gpt-6-sol` and finished 44 of 45 cards first try against ~0.87 for Sol. Those cards were picked for Luna by people, so this is optimistic.
- With `lane-kanban` and the calibrated map: 4% small, 72% medium, 16% high, 1% escalate, 7% keep. **-1.0% tokens** against today's default, **-32%** against always-top.

So on a fleet that already runs a mid model at medium effort, first-lane routing saves little; its value is the risk signal and evidence-driven escalation. The switch stays in `shadow`.

**Claude Code, 2,656 delegated subagent runs** (both machines, mostly Workflow agents): the lane policy put 22% in medium, 46% in high, 31% in keep_current, 0.4% in escalate, none in small. Priced on the same tokens, lanes cost **-3.9%** against running every one on Opus 5.5; all of it from Sonnet on the medium lane. Effort is not modelled there (most runs were at xhigh; the lanes use medium and high).

**Controlled replay**: see below.

## Promotion

Lanes change nothing on install. Claude Code gets the subagents and the CLAUDE.md block, which only tell the orchestrator how to choose. Hermes gets the switch, off. Move Hermes to `on` only when `jev lane shadow-report` on your own cards shows fewer tokens at the same first-try success (within 2 points), and with the owner's yes.
