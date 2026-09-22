# Running Jev across a Hermes fleet

A single agent with Jev is useful. A fleet of agents that all make the same cheap decisions the
same way is a different thing, and it needs a written posture rather than each lane improvising.
This is that posture, and it is portable: nothing here names a machine, a path or a model pool.

## The standing principle

**Hand Jev the picks, the rankings and the gates.** The less an agent does itself, the better it
is, and every decision a text model re-decides by hand is a decision it is paying frontier tokens
to guess at.

Jev never writes. Any change that ends with Jev producing text, a command, a coordinate or an
argument that gets executed is a change in the wrong direction, because the whole guarantee is
that Jev chooses from a closed set that code built.

## Never bypass Jev

The rule is not "prefer Jev". It is: **in work where a step gets chosen, the step is a Jev choice
or the run stops.** In GUI and browser work that means every action comes from the agent's own
table of prevalidated actions, and a run that cannot get a choice reports the blocker instead of
picking one itself.

Fail-open is not a bypass, and the difference matters:

- A fail-open answer is **exactly what the agent would have done without Jev** — routing keeps the
  current model, memory returns the original list, compaction drops nothing, skill selection
  suggests nothing, and GUI and browser use return `reobserve`.
- An answer that contradicts itself is a failure, not a reading: a Choice whose probabilities do not
  cover exactly the options offered, a chosen option that is not the maximum, a Score that disagrees
  with its own distribution — `client.ask` refuses these as `invalid_response` and the feature takes
  its fail-open path. The reply parses; the answer is what is unusable.
- A bypass is the agent choosing something itself that Jev could have chosen. That is the thing
  the rule forbids, and a fail-open path is never cover for it.

A fleet that treats fail-open as "and then do whatever you like" has no rule at all.

## Which decisions Jev owns

| Decision | Fires when | Fail-open |
|---|---|---|
| Model routing | Every routed turn | Keep the current model |
| Skill selection | A turn arrives and a skill might apply | Suggest nothing |
| Memory filtering | A retrieval returns more than a handful of passages | Return the original list |
| Turn selection | A transcript must be cut to a fixed size | Drop nothing |
| Search picking | After a search, before opening results | Screened head of the list, marked `unknown` |
| GUI action | Before each step in a desktop loop | `reobserve` |
| Browser action | Before each step in a page loop | `reobserve` |
| Escalation | A task is already judged hard | Name no seat and carry on |
| Supervision | While delegated work is running | Keep polling the transcript |

The gate to hold onto: an agent should be able to say which row a decision came from. "I decided"
without a row is the thing this whole arrangement exists to remove.

## Thresholds a fleet must not quietly change

Every threshold assumes a real, calibrated probability:

- the floor before a GUI action,
- the floor before a transcript turn may be dropped,
- the routing floors on probability mass, and "unsure is not hard".

**Substituting a model that reports its own confidence changes all of them at once, and nothing
fails visibly** — the numbers still arrive, they are just no longer probabilities. If a feature
needs a model that is not Jev, it belongs behind a different name and its own threshold, so the
calibration of the Jev paths stays intact.

## Rolling it out

1. **Shadow.** Decide and log without switching anything. A day of shadow decisions is the
   cheapest evidence a fleet will ever get, and it is the honest way to see what Jev would do.
2. **Read the log, not the silence.** A quiet log proves nothing; confirm the decisions actually
   arrived and look like decisions.
3. **Turn it on**, then watch. Per-profile settings override the fleet default, so a lane can be
   opted in or out without touching everyone else.
4. **Bound by the clock, not the count.** A decision that has not answered in its budget is a
   fail-open, not a reason to wait longer.

## Where the fleet's own facts live

The public repo holds the **loop**. Anything specific to one fleet — which machine, which
credential source, which driver or harness version, which older skills are retired, which
profiles are private, and the standing posture above — belongs in that fleet's own rules file,
not here. On Hermes that is `~/.hermes/shared/rules/<topic>-fleet.md`, and the shipped skills
point at it rather than forking.

Skills **point, they do not duplicate.** A skill that repeats a fleet's runtime facts will be
wrong within a release, and a reader will not know which copy is authoritative.

## What a fleet should write down

- which decisions Jev owns, and which it deliberately does not;
- the thresholds in force, and who may change them;
- the pointer to its own fleet rules file;
- which profiles are private, and therefore send coarse features only;
- the escalation order for hard work, and where a refusal is recorded so other lanes skip that
  seat;
- the one-line rule for the fleet's own agents: hand Jev the picks, the rankings and the gates.