# Design-choice verification receipt

## Scope and critical self-review

This is a reference example, not a live design feature or an independent review.
The actual implementation and tests were reviewed against the request to borrow
only a useful, tested idea. The useful part is bounded selection, deterministic
rendering and observed-output checks. This toy's enum already names its baseline,
so code alone solves it; no claim is made that a Jev call improves it.

The review caught a CLI ordering issue: an existing output path was refused only
after opt-in selection. A failing regression was added, the exclusive file open
moved before selection, and the same test was rerun successfully. Existing output
is now preserved without calling the provider. Red/green passes also established
the missing-module, live-selector and contrast/bounds checks before implementation.

Version numbers remain synchronized at the latest published version. The new
entry is under `Unreleased`; the repository's release steward owns release bumps.
A speculative version bump failed the existing released-changelog test and was
removed rather than inventing release notes or weakening the guard.

## Executed checks

- Python 3.9: full unittest discovery, 1,433 tests, OK.
- Python 3.11: full unittest discovery, 1,433 tests, OK.
- Python 3.14: full unittest discovery, 1,433 tests, OK, five existing skips.
- Seven new test methods cover offline selection, actual client validation with
  fake transport, enum-only outbound state, contrast/copy bounds, unknown IDs,
  malformed/missing answers, nonfinite confidence, low-confidence fallback,
  timeout, missing credentials, invalid-intent refusal, HTML escaping, CLI output
  and refusal to overwrite before a provider call.
- Twelve real headless Chrome cases passed: two presets, three widths (320, 768,
  1440), normal and maximum-length unbroken copy. The captured output is in
  [browser-results.json](browser-results.json). Every case has no horizontal or
  text clipping, correct responsive columns, contrast >= 4.5:1, zero scripts,
  zero page requests and zero page errors.
- Product desktop and editorial mobile screenshots received visual review:
  readable hierarchy, no blocking clipping or overlap found. This is sample
  visual QA, not general design-quality qualification.
- `install.py --check` followed by actual `install.py --hermes-root-only` in a
  disposable Hermes home: no warning, no profiles enabled. Imported the installed
  module with isolated Python and exercised selection and rendering. Both CLI
  previews also produced real self-contained HTML. Existing fleet/pilot install
  and all live settings were deliberately preserved.
- `scripts/check_release.py` and `git diff --check`: clean. The release scanner
  identifies the repository's pre-existing dashboard image as an unscanned binary;
  no binary was added by this change.
- Browser contexts/browser closed in `finally`; resource wrapper status returned
  no remaining lease.

## Limits

Tests used no real TypeSafe key, secret store or paid API request. Transport fixtures
are explicitly simulated and do not establish live Jev latency, accuracy, savings
or confidence calibration. The illustrative 0.85 floor is not promoted. Full WCAG
conformance and other rendering engines were not tested. No new broad skill, plugin
hook, routing default, gateway restart, production endpoint or release is introduced.
