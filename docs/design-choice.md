# Bounded design choices: a runnable example

[Jev's design-choice demo](https://x.com/thegreatest_sv/status/2108164052253917690)
illustrates a useful separation: Jev selects, code renders. This example borrows
that idea, not the post's implementation or its claim that nothing ships broken.

## Try it without a key, network or paid call

From the repo root:

```sh
python3 -m jevkit.design_choice --intent product --out product-preview.html
python3 -m jevkit.design_choice --intent editorial --out editorial-preview.html
```

Open the HTML locally. The files are self-contained: system fonts, no scripts,
images, remote styles or telemetry. Existing output files are never overwritten.
`--title` (up to 200 characters) and `--body` (up to 4,000) stay local and are
HTML-escaped. Empty copy or unknown intents are rejected before any provider call.

Two complete, compatible presets demonstrate the contract:

- `product`: grid on desktop, stacked on mobile; sans type; midnight palette.
- `editorial`: stacked layout; serif type; paper palette.

The caller chooses an existing preset as its baseline. Offline mode returns it
unchanged. This toy's intent already names that baseline, so **code alone is
sufficient**. Do not spend on Jev simply to reproduce a fact code already knows.
The reusable part is the closed-set boundary and verification, not this two-item
classification task.

## Opt-in selection, not a promoted design policy

`--live` explicitly permits a provider call and may incur charges. Use it only
with the user's authorization and an already configured key. **No live call was
used to test this example.** `select(intent, live=True)` calls `jevkit.client.ask`
once (two-second total timeout, zero retries). The enum intent passes through
`privacy.redact`; candidate descriptions are fixed public constants. No page
copy, free-form brief, CSS, paths or customer data enters the state.

Jev can return only a preset ID. It cannot supply colors, fonts, layouts, HTML,
coordinates, filenames or commands. Choosing a whole preset avoids incompatible
independent choices. The common client rejects unknown IDs and malformed/nonfinite
confidence. Missing credentials, network/provider errors, timeout and confidence
below 0.85 return the caller's baseline; the receipt names `code`, `jev` or
`fallback`. 0.85 is an **illustrative, uncalibrated policy floor**, not a claim
about design accuracy. No plugin hook, skill routing or production default changes.

To make this useful beyond the example, let caller code build several genuinely
plausible, locally vetted candidates for a non-sensitive intent. Measure Jev
against the same code-only baseline on held-out tasks before enabling selection.
Do not make arbitrary private briefs sendable by merely redacting them. Keep the
existing privacy gate and obtain explicit authority for any changed outbound data.

## Code checks and browser checks

Before rendering, code enforces the closed preset ID, copy bounds and WCAG sRGB
text/background contrast of at least 4.5:1, including the accent label. HTML
escaping, a restrictive CSP, wrapping, minimum-width-zero grid children and a
mobile stack protect this fixed template. These checks do **not** establish full
WCAG conformance, general renderer safety or subjective design quality.

Offline tests exercise the actual client validator with a fake transport and
patched credential resolver, never the real API or secret store:

```sh
python3 -m unittest discover -s tests -p test_design_choice.py -v
```

Optional real-browser QA uses an existing Node Playwright installation and browser:

```sh
node scripts/check_design_choice_browser.cjs
```

Use `JEV_PLAYWRIGHT_MODULE` for an existing package location,
`JEV_BROWSER_CHANNEL=chrome` for an installed Chrome instead of Playwright Chromium,
`PYTHON` for the desired Python interpreter and `JEV_SCREENSHOTS` for optional
screenshots. The script downloads nothing. Follow your host's resource-lifecycle
wrapper when required; it closes only its own browser and contexts in `finally`.

The check renders both presets at 320, 768 and 1440 pixels, with normal copy and
200/4,000-character unbroken text. It asserts no horizontal/clipped-text overflow,
expected responsive columns, contrast, no scripts, no page errors and no page
network requests. Twelve checks passed in a real headless Chrome run. Desktop
product and mobile editorial screenshots were also visually reviewed for hierarchy,
readability and overlap, with no blocking defect found. Browser checks cover this
template, not all conceivable content or browsers.

## What this adds, and what it does not

This is a tested reference pattern, not a new broad skill or an automatically
enabled design system. A closed set limits what a selector can invent; it cannot
prove that a valid preset looks good. Keep local render checks and visual QA.
Before claiming improved outcomes, measure preference accuracy, fallback rate,
latency/cost, order sensitivity and held-out design-quality judgments. No live
Jev quality, confidence calibration or token-savings result is claimed here.
