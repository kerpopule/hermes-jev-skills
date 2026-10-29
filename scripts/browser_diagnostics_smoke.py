#!/usr/bin/env python3
"""Real Chromium DOM/executor smoke, with synthetic decisions and no network.

Optional QA dependency: playwright. Uses an installed Chrome, never downloads a
browser. Run under your normal bounded resource owner. No Jev/provider calls.
"""
import argparse
import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('browser_runner', ROOT / 'skills/jev-browser-use/scripts/jev_browser_agent.py')
assert spec and spec.loader
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

HTML = '''<!doctype html><title>Settings</title><h1>Settings</h1>
<label><input id="toggle" type="checkbox"> Enable reminders</label>
<button id="menu" aria-expanded="false" onclick="this.setAttribute('aria-expanded',this.getAttribute('aria-expanded')!=='true')">Options</button>
<div style="height:3000px">Scrollable fixture</div>'''


class DOMAgent:
    """Synthetic decision transport; real browser input, observations and verification."""
    def __init__(self, page, decisions, *, no_op=False):
        self.browser = page
        self.decisions = iter(decisions)
        self.no_op = no_op
        self.state = {'page': {'url': page.url, 'fingerprint': 'synthetic'},
                      'history': [], 'text_calls': [], 'status': 'ready'}

    def command(self, name, body=None):
        if name == 'predict':
            self.state['decision'] = {'choice': next(self.decisions), 'confidence': .9}
        else:
            choice = self.state['decision']['choice']
            if choice in ('DONE', 'BLOCKED'):
                self.state['status'] = 'done' if choice == 'DONE' else 'blocked'
                return
            if not self.no_op:
                if choice == 'scroll':
                    self.browser.evaluate('scrollBy(0, 200)')
                else:
                    self.browser.locator('#' + choice).click(timeout=1000)
            self.state['history'].append({'kind': 'click', 'page_changed': False})
            # Reproduce the vendor text-only heuristic to verify semantic rescue.
            if len(self.state['history']) == 3:
                self.state['status'] = 'blocked'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps({'schema': 'jev.browser_diagnostics_smoke_v1',
                                           'passed': False, 'status': 'started_not_verified'}) + '\n')
    from playwright.sync_api import sync_playwright
    chrome = runner.find_chrome()
    if not chrome:
        raise SystemExit('No installed Chrome; nothing downloaded')
    receipts = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chrome, headless=True)
        try:
            context = browser.new_context(viewport={'width': 800, 'height': 600})
            context.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=HTML)
                          if route.request.url == 'https://fixture.example/' else route.abort())
            for name, decisions, no_op, expected in (
                ('controls_without_navigation', ['toggle', 'menu', 'toggle', 'DONE'], False, 'completed'),
                ('scroll_without_navigation', ['scroll', 'scroll', 'scroll', 'DONE'], False, 'completed'),
                ('no_progress', ['toggle'] * 5, True, 'blocked'),
                ('explicit_blocked', ['BLOCKED'], False, 'blocked')):
                page = context.new_page()
                try:
                    page.goto('https://fixture.example/', wait_until='load', timeout=5000)
                    agent = DOMAgent(page, decisions, no_op=no_op)
                    out = runner.run_agent(agent, allow=['fixture.example'], expect='Settings', max_ticks=5)
                    assert out['outcome'] == expected, (name, out)
                    assert out['verified'] == (expected == 'completed'), (name, out)
                    assert all(set(r) == {'observation_sha256', 'progress', 'proposed', 'executed', 'confidence', 'stagnant'}
                               for r in out['trace'])
                    receipts.append({'case': name, 'outcome': out['outcome'], 'verified': out['verified'],
                                     'stop_reason': out['stop_reason'], 'ticks': out['ticks'], 'trace': out['trace']})
                finally:
                    page.close()
            context.close()
        finally:
            browser.close()
    result = {'schema': 'jev.browser_diagnostics_smoke_v1', 'real_chromium': True,
              'synthetic_decisions': True, 'provider_calls': 0, 'network_requests_forwarded': 0,
              'cases': receipts, 'passed': True, 'owned_browser_closed': True}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
