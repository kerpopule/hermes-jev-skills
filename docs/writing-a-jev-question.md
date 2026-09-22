# Writing a question Jev can answer

A Jev answer is only as good as the question and the state you hand it. This is the part that
is easy to get wrong, because a state that reads perfectly well to a person reads to Jev as a
list of equally binding facts.

## The finding: separate the requirement from the preferences

A vendor running Jev in production (Virlo, a short-form video research tool) reported their
largest single accuracy jump did not come from a bigger model or a longer prompt. It came from
one sentence about what the question was actually asking for.

Their task: decide whether a video belongs in a stated topic. Their first version described the
brief in full — topic, tone, format, audience — and Jev read every one of those as a hard
requirement. Accuracy on a 44-video human-labelled set: **16 of 44**. Rewriting the question so
the *topic* was the requirement and everything else was explicitly a preference: **33 of 44**.
Same model, same test set, same code path.

Source: *[Jev is INSANE for Marketing](https://x.com/dsqjaffa/status/2102054148925526206)*,
jaffa (@dsqjaffa), 2026-09-21, the "AVOID FORMALITIES" section. Self-reported by a vendor
selling the product, n=44 across six intents, no published per-case breakdown. Treat it as a
strong default to try and measure, not a law.

## Why this happens

Jev scores the options you give it against the state you give it. It has no way to know which
line of your state is the binding one, so it will treat a soft attribute as a filter. Ask "which
of these is in topic" while the state also says *short, casual, vertical, for beginners*, and a
video that is on topic but formal gets downweighted by a criterion you never meant to apply.

The fix is one sentence, and it belongs in the question rather than the state:

```
Which of these belongs to the stated topic? Judge only that. Format, tone, length and
audience are preferences to break ties, not conditions to meet.
```

## Doing it for each shape

**Choice** — name the one attribute that decides, then name the attributes that only break ties.
Never leave a second attribute unlabelled; unlabelled reads as required.

**Score** — say which rubric levels are gated by the requirement and which absorb the
preferences. A score question that mixes them produces a middle score that means nothing.

**Noul** — this is where it bites hardest, because a yes/no with an unstated "and also" is
really two questions. Ask the requirement first, and ask the preferences separately, or at all.

## Making sure it worked

Keep a small labelled set — thirty to fifty cases is enough — and replay it. `evals/` shows the
shape: same cases, two phrasings, compare. A phrasing change that is not measured is a guess,
and the reason the vendor's number moved was that they had the set to see it move.

## What the client refuses, so this does not have to be remembered

`client.ask` checks the shape of every question before a request is made or a key is
resolved. A refusable question never costs a round trip; it is a caller bug, and it comes back
as a `ValueError` naming the question:

- the `type` has to be one of `choice`, `score`, `noul`;
- `instructions` has to be a non-empty string — and it cannot be the question's own name
  folded to its letters. `{"id": "blocked_on_review", "instructions": "blocked on review?"}`
  asks nothing: the id names the question, the instructions ask it;
- a `choice` needs an object of at least two options, a `score` a list of at least two levels;
- a `noul` has no criteria, so criteria written there are refused rather than silently dropped;
- the state has to encode to at most `client.MAX_STATE_CHARS`, and that is measured on the JSON
  that goes out.

The builders (`client.choice`, `client.score`, `client.noul`) enforce the same rules, and
`tests/test_question_shape.py` sweeps every question the package's features actually send —
routing, triage, mailbox, `choose`, compaction, search, rerank and skill selection — so a
question that breaks one of them fails the suite rather than reaching Jev.

## What not to do

- Do not add more detail to the state and hope the extra detail disambiguates. Detail is what
  created the problem.
- Do not soften the requirement into the question's tone. State it plainly as the requirement;
  Jev is reading it as a condition, not as a mood.
- Do not move the clarification into a system prompt or a persona. The question is what is
  scored against the state.
- Do not lower a confidence threshold to compensate for a question that is now answering two
  things. Fix the question. The thresholds in a fleet are calibrated against one question each.