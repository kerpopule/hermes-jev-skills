// Optional real-browser QA. Playwright is a test dependency, not a runtime dependency.
// Set JEV_PLAYWRIGHT_MODULE to reuse an existing package; no browser is downloaded.
const { chromium } = require(process.env.JEV_PLAYWRIGHT_MODULE || 'playwright');
const { spawnSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');

const root = path.resolve(__dirname, '..');
const generated = spawnSync(process.env.PYTHON || 'python3', ['-c', `
import json
from jevkit.design_choice import PRESETS, render, contrast
print(json.dumps([dict(preset=p, stress=stress, html=render(p,
    'W'*200 if stress else 'Good decisions. Real rendering.',
    'W'*4000 if stress else 'Jev picks a vetted preset. Code owns the layout, typography and palette. Local checks verify the rendered result.'),
    contrast=contrast(p), accent_contrast=contrast(p, accent=True))
    for p in PRESETS for stress in (False, True)]))
`], { cwd: root, encoding: 'utf8' });
assert.equal(generated.status, 0, generated.stderr);
const cases = JSON.parse(generated.stdout);
const screenshots = process.env.JEV_SCREENSHOTS;
if (screenshots) fs.mkdirSync(screenshots, { recursive: true });

(async () => {
  const browser = await chromium.launch({ headless: true, channel: process.env.JEV_BROWSER_CHANNEL || 'chromium' });
  const results = [];
  try {
    for (const test of cases) for (const width of [320, 768, 1440]) {
      const context = await browser.newContext({ viewport: { width, height: 900 } });
      try {
        const page = await context.newPage();
        const requests = [], errors = [];
        page.on('request', r => requests.push(r.url()));
        page.on('pageerror', e => errors.push(e.message));
        await page.setContent(test.html);
        await page.evaluate(() => document.fonts.ready);
        const observed = await page.evaluate(() => {
          const nodes = [...document.querySelectorAll('main,section,h1,p')];
          return { overflow: document.documentElement.scrollWidth > innerWidth,
            boxesInside: nodes.every(n => { const b = n.getBoundingClientRect(); return b.left >= -1 && b.right <= innerWidth + 1; }),
            textFits: nodes.every(n => n.scrollWidth <= n.clientWidth + 1),
            columns: getComputedStyle(document.querySelector('section')).gridTemplateColumns.split(' ').length,
            scripts: document.scripts.length };
        });
        assert.equal(observed.overflow, false);
        assert.equal(observed.boxesInside, true);
        assert.equal(observed.textFits, true);
        assert.equal(observed.scripts, 0);
        assert.equal(observed.columns, width > 600 && test.preset === 'product' ? 2 : 1);
        assert.ok(test.contrast >= 4.5 && test.accent_contrast >= 4.5);
        assert.deepEqual(requests, []);
        assert.deepEqual(errors, []);
        if (screenshots && !test.stress && (width === 320 || width === 1440)) {
          await page.screenshot({ path: path.join(screenshots, `${test.preset}-${width}.png`), fullPage: true });
        }
        results.push({ preset: test.preset, stress: test.stress, width, ...observed,
          contrast: test.contrast, accentContrast: test.accent_contrast, requests: requests.length, errors: errors.length });
      } finally { await context.close(); }
    }
  } finally { await browser.close(); }
  console.log(JSON.stringify({ passed: results.length, results }, null, 2));
})().catch(e => { console.error(e); process.exitCode = 1; });
