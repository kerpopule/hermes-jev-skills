# Opt-in bounded local action tables

`jevkit.local_goal` is an experimental selector for a caller-owned local
observe → choose → act loop. It is **not** a new default browser or desktop
driver, a native-app qualification, or an unrestricted agent. No installer
configuration, routing, gateway or serving default changes are required to use
it. Normal Co-Agent and browser-runner preferences remain unchanged.

## Why this exists

A model chooses one **whole prevalidated action id**, not an operation, target,
input and modifier in independent questions. An executor can then re-check the
live target, execute once, observe again, and use an independent postcondition
before returning to its planner. This reduces decision ambiguity and avoids a
large-model round trip for each ordinary step.

## API

```python
from jevkit.local_goal import select

result = select(
    "Reach the confirmed workflow-complete screen. Advancing setup is progress.",
    observation_id=observation_revision,
    elements=observed_elements,
    bindings={"Display name": "display_name"},
    inputs={"display_name": "Ada Test"},
    history=verified_action_history,
)
action = result["action"]  # None means do not execute a mutation.
```

Elements are dictionaries with `id`, `name`, `role`, `actions` (`CLICK` or
`TYPE_TEXT`), `visible`, `enabled`, and optional `value`. Bound text fields must
also carry a caller-verified `input_type` of `text`, `search` or `textarea`;
missing or protected types are refused. Secure native text roles and explicit
password types are refused even when the displayed label is innocuous. Adapters
must preflight the authorized target for protected fields: a masked value or a
generic textbox role does not prove that a field is safe. The synthetic browser
adapter checks protected-field presence without reading field contents.
The element id comes
from the real observation. `bindings` maps an exact observed field label to a
planner-supplied input key. The model cannot supply typed text, coordinates,
selectors, commands or arguments. Ambiguous bindings and oversized action
tables are refused rather than truncated.

The caller must enforce an exact authorized target/origin, re-check freshness
immediately before each mutation, maintain a deadline and step budget, never
retry a mutation blindly, and verify the **actual requested effect**. A done
label or model judgement alone is not proof. Return control to the planner on
an unsupported or unsafe operation. This module does not launch or drive an app.

## Outbound and refusal boundary

This is opt-in and requires an already-authorized, non-sensitive screen. No
secure fields, credentials, payment prompts or customer records. Existing
`choose` → privacy → `client.ask` credential and response validation are reused.
No new endpoint or key flow is added. Label/role/id regions, the non-sensitive
goal/history, and candidate descriptions go to Jev. **Unlike the desktop
label-only path, a supplied non-sensitive bound text value is explicitly
included in that candidate description.** Unbound field values are omitted;
sensitive/contact-shaped bindings are rejected before any request. Do not use
this module where that outbound contract is not authorized.

Consequential labels are conservatively excluded, not approved by inference.
The existing chooser's confidence floor is unchanged. The module additionally
rejects stale observation ids, unknown action ids, nonfinite/low confidence,
duplicate element ids, secure roles, ambiguous fields and privacy failures.
`reobserve`/`abstain` produce no executable action. Network failure never causes
a guessed mutation.

## Qualification evidence and remaining limits

See `evals/local-goal/receipt.json` for sanitized synthetic-fixture measurements.
The measured full loop used pinned Arc CUA 0.1.1 as its browser observation,
freshness and execution layer. Owned headless Chrome and a loopback-only
fixture server were used; no user browser or app was attached to. The tested
optimizations included a caller-specified transient-state wait, shorter quiet
settling and deterministic verification. **Those executor features are not
implemented by this selection-only module.**

These are warm engine measurements, excluding Chrome launch, navigation,
planner/tool dispatch and human-facing delivery. They do not establish a
fleet-wide end-to-end speedup, real application quality, or parity with any
published demo. The Ultrafast comparator used an identical owned CDP transport
instead of browser-harness IPC and a common confidence gate; it is a controlled
policy comparison, not a claim about its default installed configuration.

Native qualification remains a separate real-device gate. A locked desktop is
not proof of driver failure and must never be bypassed. All local mutation
experiments require owned bounded resources and explicit end-state verification.
