#!/usr/bin/env python3
"""Finite offline checks with sanitized, byte-bound receipts."""
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'evidence/ecosystem'

def main():
    OUT.mkdir(parents=True, exist_ok=True)
    checks = []
    commands = [
        ('unittest', [sys.executable, '-m', 'unittest', 'discover', '-s', 'tests']),
        ('release', [sys.executable, 'scripts/check_release.py']),
        ('diff', ['git', 'diff', '--check']),
    ]
    for name, command in commands:
        run = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=240)
        log = (run.stdout + run.stderr).replace(str(Path.home()), '<home>')
        (OUT / (name + '-final.log')).write_text(log)
        match = re.search(r'Ran (\d+) tests in ([\d.]+)s', log)
        row = {'name': name, 'command': ['python' if arg == sys.executable else arg for arg in command],
               'exit_code': run.returncode, 'log': name + '-final.log'}
        if match:
            row.update(tests=int(match.group(1)), seconds=float(match.group(2)))
        checks.append(row)
        print(json.dumps(row), flush=True)
    paths = ['jevkit/search.py', 'jevkit/gate.py', 'jevkit/cli.py',
             'skills/jev-browser-use/scripts/jev_browser_agent.py',
             'scripts/browser_diagnostics_smoke.py',
             'tests/test_browser_diagnostics.py', 'tests/test_search_planning.py',
             'tests/test_gate_failure_boundary.py']
    report = {'schema': 'jev.ecosystem_checks_v1',
              'checked_at': datetime.now(timezone.utc).isoformat(),
              'python': sys.version.split()[0], 'checks': checks,
              'artifact_sha256': {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in paths},
              'passed': all(row['exit_code'] == 0 for row in checks),
              'paid_evaluation': False, 'independent_review': False}
    (OUT / 'checks.json').write_text(json.dumps(report, indent=2) + '\n')
    return 0 if report['passed'] else 1

if __name__ == '__main__':
    raise SystemExit(main())
