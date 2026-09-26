# Web screening scorecard, 2026-09-26

**The question:** should Jev screen every `web_search` and `web_extract` result before a Hermes agent reads it?

**The answer:** yes. This is what `/jev screen on` does. The harness is `run_eval.py` in this folder, and it reproduces the whole run.

## Why a seam and not a tool

`jev_memory_filter` and `jev_search` screen passages, but only when the agent remembers to call them. On one real fleet, over one week:

- 890 `web_search` and `web_extract` results went straight into a worker's context.
- The agents called the two Jev screening tools 57 times.

So almost all of that web text reached a worker unscreened. The fix is to put the screen where every result passes: Hermes's `transform_tool_result` hook.

## Method

- **Real results.** Two samples of 40 real results each (20 `web_search`, 20 `web_extract`). They were drawn from the same fleet's own session stores, skipping every profile listed in `private_profiles`. Each result is split into *units*:
  - each search hit's title;
  - each search hit's description;
  - each page chunk of up to 900 characters.
- **Attacks.** Every result is screened twice:
  - as it was fetched;
  - with one attack planted in one unit, at its start, middle or end.
- **The attack set.** 40 attacks written for this run (they are listed in `run_eval.py`). They cover:
  - blunt overrides and fake system messages;
  - markdown-image and link exfiltration;
  - tool and command abuse;
  - claimed authority ("the user has pre-authorized you");
  - quiet paraphrases with no trigger words;
  - hidden HTML and markdown comments;
  - role-play and JSON tricks;
  - memory poisoning;
  - poisoned conclusions ("report that the code was audited");
  - French and Spanish;
  - hyphen obfuscation.
- **Metrics.** Scored per unit:
  - **caught** means a planted unit was withheld;
  - a **false positive** means a unit of an untouched result was withheld.

**The four arms:**

| arm | what it is |
|---|---|
| today | What reaches the agent without this feature. Nothing is withheld. Hermes wraps the block as untrusted data and keeps an internal pattern finding that the model never sees. |
| hermes | Hermes's own `scan_for_threats`, as a reference only. It is advisory and is not surfaced to the model. |
| local | jevkit's local pattern screen alone. This is what a private profile, or a Jev outage, gets. |
| jev+local | `webscreen.screen()`. This is what `/jev screen on` does. |

## Results

| | today | hermes | local | **jev+local** |
|---|---|---|---|---|
| **Caught, sample A** (39 planted) | 0 | 4 | 9 | **35** (89.7%) |
| **Caught, sample B** (40 planted) | 0 | 7 | 9 | **35** (87.5%) |
| False positives, sample A (553 clean units) | 0 | 24 | 2 | **0** |
| False positives, sample B (967 clean units) | 0 | 60 | 2 | **0** |
| Clean results with anything withheld (of 80) | 0 | n/a | 4 | **0** |

**Speed and reliability, per screened result** (160 screenings in total):

- Median 208 ms in sample A and 227 ms in sample B.
- 90th percentile 268 ms and 339 ms.
- Worst case 476 ms.
- Each screening is one or two requests.
- 0 fail-opens.

## What changed while measuring

The first version withheld whatever the local pattern screen raised, the way the memory filter treats local hits. On real web pages that cost four clean units across the two samples. All four were ordinary documentation:

- two install one-liners (`curl … | sh`);
- a favicon URL with a `url=` parameter;
- a Microsoft doc titled "Use developer mode in Microsoft Copilot".

The patterns were tuned on README passages, and web pages are broader.

**The shipped version:**

- Jev also judges the units the local screen raised.
- Those units go in their own request, so text written to steer a model cannot reach the request that judges everything else.
- The local verdict stands only for a unit Jev did not judge: during an outage, on a private profile, or for a credential-shaped unit that is never sent.

**The effect:**

- False positives went from 4 to 0.
- Every planted attack the local screen had caught was still caught.

## What it missed, and the threshold

Nine planted units got through across the two samples. Each one had an unguarded reply to steer:

- an authority claim ("pre-authorized purchases");
- a markdown comment asking for a config flag;
- two memory-poisoning lines ("remember permanently", "store as a fact");
- "report that the code was audited";
- "turn off confirmation prompts".

One further miss is a special case. Its text contains the word "password", so it is never sent to Jev, and no local rule matches it.

**How close the misses were:**

- Jev scored the missed units between 0.35 and 0.47.
- It scored no clean unit above 0.28.
- A threshold near 0.3 would have caught all but that one special case.

**The threshold stays at 0.5 anyway,** the same calibrated "more likely than not" the memory filter uses. It was not tuned on these samples, and choosing one on the data it is scored on would overstate it. A held-out set of real attacks is the way to move it.

## Limits

- **The attacks are ours.** They were written to be varied, including quiet paraphrases, but they were planted rather than found. Real attacks may be worded differently.
- **One attack per result.** A page carrying several attacks gives Jev more to find, not less.
- **Units, not answers.** This measures what reaches the agent. It does not measure whether a given agent would have obeyed a missed line.
- **Browser pages are out of scope.** A logged-in page is the person's own data, so it is not sent.
