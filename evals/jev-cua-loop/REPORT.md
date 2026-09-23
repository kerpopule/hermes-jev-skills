# Jev CUA loop adaptation — 2026-09-23

Reference: [ronadin2002/jev-cua](https://github.com/ronadin2002/jev-cua), commit `098e9348fbfc7afae61575960c15cdaaae960b0b`. The checkout contains no tracked LICENSE/COPYING/NOTICE; this adaptation copies no Swift code. The README states its demo preview is 2× speed and not a performance benchmark. We did not build, install, launch, sign, use microphone/camera/Accessibility permissions, or call its paid API. The upstream build script signs a macOS app, which is unnecessary for this Python concept adaptation.

## Comparison and choice

The upstream implementation uses an AX traversal bounded by time and element count, then chooses an action, checks cancellation, reobserves, notes unchanged screen signatures and verifies completion separately. Its catalogue groups large candidate lists, but groups would add model calls to our 32-candidate contract; its text selection makes several additional model calls. Our existing Python runner already has a one-chooser-call loop, `--plan`, a cache, closed IDs, stable semantic IDs, a 26-element shortlist, per-step timing, and a driver-owned element token. It only stopped repeated **reobserve** choices, not repeated *delivered but ineffective* clicks. We adapted the unchanged-state principle with a local hash including the title, all observed controls, field values and selected states, ignoring ephemeral tokens/indices. Two unchanged attempts of the *same* mutation end `stalled_action` before the third chooser call. Changed state, including a text value, resets the no-progress count. An unknown selected ID ends `invalid_choice` without dispatch.

The hash stays local and is never logged or sent to Jev. The closed candidate table, sensitivity filtering, confidence floor, fresh observation, exact driver token binding, action budget, existing cancellation path and final independent `--expect` verification remain. A delayed asynchronous update may arrive after this guard stops: the result is **unverified**, not failure proof. A single planned click continues to mean delivered input rather than a verified intermediate outcome; final `--expect` is still required. No no-confirmation policy or desktop-control mechanism is imported from upstream.

## Keyless synthetic comparison

Source baseline: `b34aea789006691d6f3c17f0fe918cf916a67c18`; candidate: this change. Same synthetic window/AX button (Library), same Jev response selecting `click:library` at confidence 0.91, same driver acknowledgement with no screen effect, 10-step limit, 100 in-process runs per arm under Python's `time.perf_counter_ns`. `observe`, `jev_choose` and driver were mocked; no GUI, network, Jev inference or paid call occurred. The harness is `benchmark.py` in the task workspace and loads baseline from `git show HEAD:skills/jev-computer-use/scripts/jev_gui_agent.py` before commit; isolated `test_gui_agent.py` regressions cover the essential result.

| Variant | Median local elapsed | Chooser calls | Observations | Action calls | End |
|---|---:|---:|---:|---:|---|
| Baseline | 0.4666 ms | 10 | 10 | 10 | `budget` |
| Candidate | 0.1392 ms | 2 | 3 | 2 | `stalled_action` |

These sub-millisecond local measurements are mock harness overhead, **not live speed or accuracy**. The only supported savings claim is fewer attempted calls on the synthetic no-progress trace. Changing-screen traces are checked separately by unit tests. Live TypeSafe/driver speed was out of scope (would require paid inference and a private desktop screen).
