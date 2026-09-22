# Plan-quality scorecard, 2026-09-22

Does `jev plan` actually plan? Measured live, and the answer was **no for one command in ten** —
not because the model failed, but because the prompt never said which keys exist, so it invented
`press_key: "volume down"`, and one step outside the vocabulary rejects the whole plan by design.
The prompt now names the keys. Method and the script that reproduces every number here:
`scripts/measure_plan_quality.py` (11 ordinary computer-use commands, one call each, ~$0.02 per
round, raw replies kept and every fallback diagnosed down to the step that killed it).

## Why this was worth counting

`parse_response` rejects a plan if a single step is outside the vocabulary, deliberately: the
steps after a bad one were written assuming it happened. The cost nobody had counted is the
fallback — a single `goal` step, which is exactly the planning the feature exists to do. A
command that falls back is a second of waiting plus an agent loop that has to work the command
out itself.

**Bar, stated before the numbers:** a command that should be plannable has to produce a plan, and
no wording change may drop a step the command asked for.

## Round 0 — before: 20 of 22 planned

Two runs over the 11 commands, live:

| | result |
|---|---|
| planned | **20 / 22** (90%) |
| fallbacks | 2, both `schema_mismatch`, both the same command, both runs |
| killed by | `press_key` at position 3 of 3, target `"volume down"` |
| median latency | 1144 ms |

`open the Sound settings pane and turn the volume down one notch` died on its last step. The
other two steps in that same reply (`open_app "System Settings"`, `click "Sound settings pane"`)
were clean — the rejection took them with it. The model had no reason to know `"volume down"` is
not a key: the prompt's only example of `press_key` was `"return"`, `"escape"`, `"tab"`, `"cmd+t"`.

## Round 1 — naming the keys, and the regression that caught

Adding the key list and licensing the model to leave a step out when it could not be expressed
took the set to **22 / 22** — and broke something else, reproducibly, 2 runs out of 2:

| command | before | after round 1 |
|---|---|---|
| `scroll down three screens and take a screenshot` | `scroll` + `press_key cmd+shift+3` | **`scroll` only** |

The licence to omit ("or leave that part of the command out") was read as licence to drop an
action that *was* expressible — a screenshot is `cmd+shift+3`, a key this vocabulary can press.
Round 1 also had `press_key f11` for the volume command: my own key list made `f1`-`f12`
available, and the model reached for f11, the volume-down key on a non-Apple keyboard.

That is the shape of prompt work: the fix for one case becomes the bug in another, and only a
replayed set tells you which.

## Round 2 — shipped: 33 of 33 planned

Final wording: the key list stays, the omission licence is gone, `f1`-`f12` are scoped ("ordinary
function keys, use one only when the command asks for that function key"), and volume and
brightness are explicitly a click on the control or a menu path. Three runs, live:

| | result |
|---|---|
| planned | **33 / 33** (100%) |
| fallbacks | 0 |
| screenshot step | retained in all 3 runs |
| volume command | `open_app System Settings` → `click Sound settings pane` → `click Volume down button`; the same 3 steps in a separate 3-run probe (3/3) |
| median latency | **1181 ms** (against 1115 ms before — the prompt is ~90 tokens longer, so ~66 ms per plan) |

## What the plans look like, spot-checked

One run's steps, verbatim, because "planned" is not the same claim as "correct":

```
turn the Bluetooth off, then back on      open_app(System Settings) click(Bluetooth sidebar item)
                                          click(Bluetooth toggle switch) wait(1) click(toggle switch)
scroll down three screens and take a shot scroll(down x3) press_key(cmd+shift+3)
find the word invoice in this document    press_key(cmd+f) type_text(search field | invoice)
on System Settings: dark mode             open_app(System Settings) click(Appearance) click(Dark mode radio button)
multiply 47 by 19 in Calculator           open_app(Calculator) click(4) click(7) click(multiply) click(1) click(9)
```

Two of these would not do what the person asked: the Calculator plan never presses `=`, and one
run's `rename the selected file in Finder` starts with a `click` where the same model on another
round started with `open_app Finder`. **This eval counts plans, not correct plans** — judging the
latter needs the runner and a person watching a screen, and there is no honest way to derive it
from the reply.

## The honest limits

- **11 authored commands, not fleet traffic.** They are ordinary read-only work, chosen so that
  every one *should* plan; none of them asks to send, pay or delete, because those are answered by
  `enforce_never_send` and its own tests, and mixing the two questions would make this number
  meaningless.
- **One failure mode across 66 live replies**, and it is the one that was then fixed. A
  `schema_mismatch` from some other boundary — a 12-step command, a target the runner refuses — is
  not represented here.
- **Run-to-run variation is real.** Temperature is 0, but the same command came back as six clicks
  in one round and two steps in the next. Kinds were stable within round 2's three runs, which is
  not the same as stable across days.
- **The prompt is now longer.** ~90 extra tokens per plan, measured at ~66 ms of extra latency,
  once per task. It buys a class of commands back from an unconditional fallback.
- **The picture is of one model** (`google/gemini-2.5-flash` by default, whatever
  `TEXT_MODEL`/`JEV_PLAN_MODEL` says). A stronger one would have less need for the key list; a
  weaker one would need more.

## What would reopen it

A **repair round trip**: on `schema_mismatch`, ask once more with the rejection fed back ("step 3
was rejected: `press_key` target must be one of …"), which converts a total loss into a plan for
one extra call. Not built, because the measured failure rate on this set is now 0 of 33 and an
extra call is a real cost — `scripts/measure_plan_quality.py` measures the failure rate whenever
someone wants it back, and the repair is only worth writing when that number is not zero.

## Reproduce

```bash
python3 scripts/measure_plan_quality.py                  # 11 commands, 1 run, ~$0.01
python3 scripts/measure_plan_quality.py --runs 3 --json /tmp/plan-quality.json
```

The script prints, per fallback, the exact step that killed the plan — so this file can be
re-derived rather than trusted.