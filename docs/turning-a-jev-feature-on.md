# Turning a Jev feature on without breaking something

Every feature in this repo is cheap enough to run on every turn. That is the appeal and it
is also the trap: a thing that runs everywhere fails everywhere. These are the rules that
came out of putting routing, compaction, handoff, triage and skill selection onto a fleet
that real people and one real customer depend on. Each one is here because it was learned
the expensive way.

## 1. Shadow first, and mean it

Three modes, default to the middle one:

| mode | decides | records | changes behaviour |
|---|---|---|---|
| `off` | no | no | no |
| `shadow` | yes | yes | **no** |
| `on` | yes | yes | yes |

In `shadow` you pay the full cost and the full latency and get a complete log of what the
feature *would* have done, while the system behaves exactly as it did yesterday. Then a
person reads a batch and decides whether it earned the switch.

The first routing policy that felt right escalated **89% of turns** to the expensive tier.
Shadow mode is the only reason that cost nothing — it would have been roughly a 7×
increase, $140 → $1,014 a week. Nobody reviews a policy they believe is already correct, so
the review has to happen while the policy is still inert.

## 2. Benchmark your benchmark before you believe it

The skill picker scored **4/8** the first time it was measured, with three false
suggestions on ordinary conversation. That number was nearly reported as "not good enough
to ship". It was wrong twice over:

- the harness forgot to pass the profile's **disabled-skills list**, so the picker was
  being scored for choosing skills that are switched off on that profile
- several "should pick nothing" labels were simply bad. *"What time is my next meeting?"*
  was marked as needing no skill. It obviously wants the calendar skill.

Run correctly, the same picker scored **7/8 with zero false suggestions**.

A bad harness and a bad model look identical from the outside. Before you trust a
disappointing score, re-derive it: does the harness run the code the way production runs
it, and are the labels defensible to someone who disagrees with you?

## 3. A config key that can default to "off" will

Confidential handoff mode was first wired to a plugin config key. That host exposes no
plugin-config API at all, so the lookup raised, the exception handler swallowed it, and the
setting returned its default — **off** — silently writing customer data to disk against a
rule that forbade it.

This was the **third** silent-default failure in the same project. The other two: a tool
enabled in four config lists that was never handed to the model because a `check_fn`
returned False, and a fleet switch that read `off` because nothing had ever set it.

So the switch for anything that must not fail quietly is a **marker file**:

```python
def confidential_here() -> bool:
    if os.environ.get("HANDOFF_CONFIDENTIAL", "").lower() in ("1", "true", "yes", "on"):
        return True
    return (handoff_dir() / "CONFIDENTIAL").exists()
```

One `ls` proves it. It cannot be swallowed by an exception handler, and it travels with the
directory it protects. Keep the config key too — just never let it be the only answer.

## 4. Absence of errors proves nothing

A quiet log is not evidence a feature is running. On one deployment the handoff plugin was
enabled in config, the code was correct, the directory existed, and no error appeared
anywhere — and it had never once run, because plugin discovery looks in the *profile*
directory and the plugin was installed at the root.

The only proof is a command that enumerates what is live:

```bash
hermes plugins list          # is the plugin loaded?
hermes -p <profile> doctor   # is the tool actually available to the model?
```

`hermes tools list` will not tell you about tools registered by module import, and a log
will not tell you about a feature that never started. Ask the thing that has to answer.

## 5. Prove it where it runs, not in your shell

A unit test and a hand-run command both pass in an interactive session, which has your
environment, your PATH and an unlocked keychain. A scheduler has none of that guaranteed.

The honest test for triage was: take one benign, already-processed message, mark it
unprocessed, let **launchd** run its own cycle, and read the record it wrote.

```json
{"route": "queue", "sent_to_jev": true, "latency_ms": 491, ...}
```

`sent_to_jev: true` from a job the scheduler started is the only thing that proved the
secret store was reachable from where the code actually lives. Everything before that was
proof about a shell.

## 6. Latency is the cost that lands on every turn

Cost per call is the number people quote. Latency is the number people feel.

The skill picker is excellent and costs $0.0003 a turn — and added **2.8 seconds to every
turn**, including "ok". Most turns in a chat are acknowledgements, and the answer for those
is always "no skill". So they are answered locally, for free:

```python
def looks_trivial(turn: str) -> bool:
    words = [w.replace("'", "") for w in re.split(r"[^A-Za-z']+", turn.lower()) if w]
    if len(words) > 6:
        return False
    return all(w in _ACK_WORDS for w in words)
```

Mean added latency went from ~2800ms to **773ms**, with two thirds of turns never touching
the network.

Note what the test is *not*: length. `"open settings"` is two words and a real request;
`"yes go ahead"` is three and is not. And note the asymmetry — a wrong **skip** makes the
feature quietly do nothing, a wrong **ask** costs half a cent. So the vocabulary stays
narrow and anything unrecognised goes to Jev.

## 7. Ship the implementation, not just the loop

`jev-browser-use` shipped a `SKILL.md` **and** a runner. `jev-computer-use` shipped the
`SKILL.md` alone — a careful description of an observe → choose → act cycle, with nothing to
execute.

An agent on the fleet read that skill, found no implementation, and did the one thing the
skill explicitly forbids: it improvised its own GUI control and wrote a 500-line runner from
scratch. It was good work. It should not have had to happen.

If a skill describes a procedure precisely enough to be followed, someone will follow it. If
following it requires a script, ship the script — or say plainly in the skill that there
isn't one and what to do instead.

## 8. Fail open, or do not ship it

The pipeline worked before your feature existed. It must work if your feature breaks.

No key, no network, a timeout, a malformed answer, an answer that contradicts itself
(`invalid_response`: a distribution that misses an option, mass that does not sum to one, a
chosen option that is not its own maximum, a score that disagrees with its spread), the toolkit
not installed at all — every one of those ends with the work done exactly as it would have been,
and the process exiting zero. Every entry point returns `None` rather than raising, and the
caller reads `None` as "no opinion".

This is not garnish. A classifier is an *opinion about* the work, never a *dependency of*
the work. The moment an opinion can stop mail being collected, you have made the system
worse than the regex it replaced.

One corollary that is easy to miss: **bound the work by the clock, not just the count.** A
triage step capped at 40 messages with a 6-second timeout is 240 seconds, inside a
subprocess its parent kills at 180. A kill mid-loop lost every message already marked seen,
because dedupe was written before routing. The count cap felt like a limit and was not one.
