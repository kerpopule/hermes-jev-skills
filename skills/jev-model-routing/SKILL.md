---
name: jev-model-routing
description: Use to pick the cheapest model that is good enough for a turn — choosing a model, delegating a sub-task to a sub-agent, cutting model spend, or setting up and tuning Jev routing pools.
version: 0.1.0
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

## Asking directly (any agent)

Before delegating a task or spawning a sub-agent, ask which model should get it:

```bash
jev route --prompt "<the task, in the person's words>" --current "<provider:model you are on>"
```

Use `model_id` from the reply. `routed: false` means stay where you are; `reason` says why. Relay `notice` if the person likes to see routing.

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
- The same difficulty answer can also set the reasoning effort for the request: add `"effort": {"enabled": true}` to `routing.json` and the plugin maps the 0–3 score onto `low / medium / high / xhigh` (or your own `levels` list) and stamps it onto the outgoing call — no second Jev request, off by default, fail-open, and your `/effort` override still wins. Works in `on`, `shadow` and `off` alike; the pick is logged as `"kind": "effort"` in `jev-decisions.jsonl`.
- Template turns are not routed: anything starting with a `skip_prefixes` entry (`[kanban]`, `[SESSION HANDOFF`…) or from a `skip_session_prefixes` session (`cron`) keeps the model its profile or job was configured with.
- Large context (over ~32k tokens): never switches to a cheaper model, because rebuilding the prompt cache costs more than it saves.
- Turns that look like they contain secrets, and any profile listed in `private_profiles`, send Jev only coarse features (length, code present, risk words), never text. Those turns, and a profile with `mode: features`, also opt out of the merged request below.
- Its three questions normally travel in the **same request** as skill selection's stage 1 (`jevkit/turn.py`), because Jev charges per request and not per question, and the connection underneath is pooled (a fresh TLS session per call used to be ~275 ms of the ~520 ms a decision cost). Measured live 2026-09-21/22: one question ~180-250 ms warm, and **1784 ms → 672 ms** per turn that needs both, 3 requests → 2. Each feature still reads its own answers through its own thresholds. `/jev merge_requests off` separates them again.
- Jev down, slow (2.5 s budget) or malformed: current model, no delay beyond the budget. An answer that contradicts itself — a spread that does not cover the options, mass that does not sum to one, a chosen option that is not the maximum, a score that disagrees with its own distribution — is refused as `invalid_response` and lands here too.

## Tuning

Decisions are logged without prompt text to `<hermes home>/logs/jev-decisions.jsonl`. Run in `shadow` for a day, read which tier real turns land in, then move models between pools. Change thresholds from your own traces, never from a hunch.
