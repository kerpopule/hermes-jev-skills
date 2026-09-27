---
name: jev-model-routing
description: Use to pick the cheapest good-enough model or effort for a turn or a delegated task (lanes small to escalate), to decide continue/retry/verify/escalate/complete after each cycle, or to tune routing.
version: 0.2.0
license: MIT
metadata:
  hermes:
    tags: [jev, typesafe, model-routing, cost]
---

# Model routing with Jev

Jev reads a turn and answers three questions in one ~0.4 s request: how hard is it, what kind of work is it, and would a mistake be costly. Code then walks your pool for that tier and specialty and takes the first model that fits (images, context size). You do not pick models by feel; you ask.

## On Hermes it is automatic

With the `hermes-jev` plugin enabled, each fresh user turn is routed once, before the first model call. Tool-loop follow-ups reuse that decision. Switches, per profile:

```
/jev                    status
/jev routing shadow     decide and log, but do not switch (start here)
/jev routing on         switch models
/jev routing off
/jev notice on          show "[Jev] medium · coding → kimi-k2.7-code · confidence 0.97" on routed replies
```

A plugin can swap the model, not the provider connection. On OpenRouter that still means every vendor (DeepSeek, GLM, Kimi, MiniMax, Grok, Qwen, Gemini, GPT). If you run `/model` yourself, your choice wins and Jev stays out of the way.

For a Hermes `custom` provider, the plugin cannot infer the backing models.dev provider. It now keeps the current model and logs `custom provider needs an explicit provider_aliases.custom` instead of blaming an unrelated pool. If and only if that endpoint actually serves the pool's models, set `"provider_aliases": {"custom": "venice"}` (replace `venice` with the real pool prefix) in `routing.json`. Check the endpoint and every pool model before enabling routing; an alias is an operator assertion, not cross-provider discovery. This does not edit any live routing mode.

## Asking directly (any agent)

Before delegating a task or spawning a sub-agent, ask which model should get it:

```bash
jev route --prompt "<the task, in the person's words>" --current "<provider:model you are on>"
```

Use `model_id` from the reply. `routed: false` means stay where you are; `reason` says why. Relay `notice` if the person likes to see routing.

## Lanes: delegating a task, and every step after it

For work you hand to a sub-agent or worker, finish with the smallest model and lowest effort that still gets it right. Jev decides; it never writes code, patches or designs.

```bash
jev lane classify --task "<the work, in the person's words>"        # first lane + model/effort
jev lane step --task "..." --lane <lane> --attempt <n> \
    --run "<test cmd>" --run "<lint/typecheck cmd>" --scope "<path glob>"   # after each cycle
```

| Lane | Claude Code (subagent) | Hermes Kanban card (default map) |
|---|---|---|
| `small` | `jev-lane-small`: Haiku, low | `gpt-5.6-luna`, medium |
| `medium` | `jev-lane-medium`: Sonnet, medium | `gpt-6-sol`, medium (today's default) |
| `high` | `jev-lane-high`: Opus, medium | `gpt-6-sol`, medium |
| `escalate` | `jev-lane-escalate`: Opus, high | `gpt-6-astra`, high |

`jev lane targets --host hermes` shows the map in force; `<hermes root>/jev/lanes.json` (or `~/.config/jev/lanes.json`) overrides any field. The Hermes map was calibrated on one fleet's own history (see `docs/lanes.md`); re-measure yours with `jev lane replay-build` / `replay-report`.

- **One request, all questions.** `classify` asks the lane (with an `other` escape: work a person should see first), security sensitivity and underspecification together. Code applies the thresholds: a `small` pick needs 0.7 confidence; a `medium` pick below 0.5 goes to `high`; security ≥ 0.7 is at least `high`. `keep_current` means keep the model you had (do it yourself, or ask).
- **Code first.** A model the person named, two failed attempts, or your own security-path check decide without asking Jev.
- **Deterministic checks first.** `step` runs the tests, compiler, type checker and linter you name and reads `git diff`. A failing check is `retry` (and `escalate` once the same lane failed twice); files outside `--scope` are `retry`; unrun checks are `verify`; security files changed on small/medium are `escalate`. Jev is asked only what is left: is it implemented, is it in scope, what next.
- **Escalate one lane at a time, on evidence only.** `escalate` from the top lane returns `person`.
- **Complete is earned.** `complete` is refused (becomes `verify`, with `complete_refused`) unless the checks ran and passed and the diff stayed in scope. Say when a check failed; never hide it.
- Only the tail of each long check output goes to Jev. Never compact or filter the agent's own reasoning.
- Jev down: `classify` keeps the current model, `step` says `verify`.

On Hermes, `jev lane shadow` (from cron) classifies new Kanban cards and logs what it would choose; `/jev lanes shadow|on|off` is the switch and `<hermes root>/jev/LANES_OFF` wins. `on` sets the card's model and effort before dispatch; turn it on only after `jev lane shadow-report` shows fewer tokens at the same first-try success, and with the owner's yes.

## The pools

`jev models list` shows every model this machine can call (the models.dev catalog, filtered to providers you hold a key or login for) with price, context and abilities. Pools live in `~/.hermes/jev/routing.json` (or `~/.config/jev/routing.json`):

```json
{"tiers": {"simple": {"general": ["openrouter:deepseek/deepseek-v4.1-flash"], "coding": ["..."]},
           "medium": {"general": ["..."], "coding": ["..."], "research": ["..."], "writing": ["..."], "vision": ["..."]},
           "hard":   {"general": ["..."], "coding": ["..."]}},
 "exclude": ["*:free"], "private_profiles": ["billing"], "mode": "redacted-text"}
```

- `jev models suggest --write` creates a first draft from price bands. Then edit: order matters, first fit wins.
- Specialties are `general`, `coding`, `writing`, `research`, `vision`. A missing specialty falls back to `general`. A pool never falls down a tier, only up.
- When the person names a model they like for something, put it first in that pool. Do not invent model ids: copy them from `jev models list --search <name>`.

## Guarantees you can rely on

- Hard is earned: it needs real probability mass on "substantial" or "expert" (0.6 by default), read from the per-level spread Jev returns, never from an averaged score.
- Unsure is not hard. An unsure answer about a harmless turn keeps the current model; about a risky turn it picks medium.
- Risk words (production, delete, migration, security, payment, legal…) set a floor of medium, however short the prompt. They do not buy the hard tier on their own.
- Jev judges the ask: a long turn is read as its opening plus, mostly, its end (`ask_chars`). Boilerplate in the middle is not what gets scored.
- Reasoning effort is off by default and only writes the standard `reasoning_effort` field when routing is `on`. Opt in with an exact provider:model capability map, for example `"effort": {"enabled": true, "levels": ["low", "medium", "high", "high"], "models": {"openrouter:your-verified-model-id": ["low", "medium", "high"]}}`. Replace the example ID with a model actually verified to accept those levels; `xhigh` is not presumed supported. The requested level must appear in the exact model's allowed list, and an existing `reasoning_effort` or `extra_body.reasoning` always wins. Shadow/off never mutate requests. The pick reuses the routing difficulty answer without another Jev call; unsupported or malformed configuration fails open. No fleet effort setting or live routing is activated by installation.
- Template turns are not routed: anything starting with a `skip_prefixes` entry (`[kanban]`, `[SESSION HANDOFF`…) or from a `skip_session_prefixes` session (`cron`) keeps the model its profile or job was configured with.
- Large context (over ~32k tokens): never switches to a cheaper model, because rebuilding the prompt cache costs more than it saves.
- Turns that look like they contain secrets, and any profile listed in `private_profiles`, send Jev only coarse features (length, code present, risk words), never text. Those turns, and a profile with `mode: features`, also opt out of the merged request below.
- Its three questions normally travel in the **same request** as skill selection's stage 1 (`jevkit/turn.py`), because Jev charges per request and not per question, and the connection underneath is pooled (a fresh TLS session per call used to be ~275 ms of the ~520 ms a decision cost). Measured live 2026-09-21/22: one question ~180-250 ms warm, and **1784 ms → 672 ms** per turn that needs both, 3 requests → 2. Each feature still reads its own answers through its own thresholds. `/jev merge_requests off` separates them again.
- Jev down, slow (2.5 s budget) or malformed: current model, no delay beyond the budget. An answer that contradicts itself — a spread that does not cover the options, mass that does not sum to one, a chosen option that is not the maximum, a score that disagrees with its own distribution — is refused as `invalid_response` and lands here too.

## Tuning

Decisions are logged without prompt text to `<hermes home>/logs/jev-decisions.jsonl`. Run in `shadow` for a day, read which tier real turns land in, then move models between pools. Change thresholds from your own traces, never from a hunch.
