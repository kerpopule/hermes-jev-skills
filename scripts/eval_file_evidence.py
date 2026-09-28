#!/usr/bin/env python3
"""Reproducible offline baseline. No model, secret store, network or live savings claims."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from jevkit import file_evidence, test_triage  # noqa: E402


def evaluate(fixture):
    data = json.loads(fixture.read_text())
    scout_rows, failure_rows = [], []
    with tempfile.TemporaryDirectory(prefix='jev-evidence-eval-') as tmp:
        root = Path(tmp).resolve()
        for case in data['scouting']:
            folder = root / case['id']
            folder.mkdir()
            for name, text in case['files'].items():
                target = folder / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text)
            out = file_evidence.scout(str(folder), list(case['files']), case['query'])
            eligible = {r['path'] for r in out['evidence']}
            selected = {r['path'] for r in out['evidence'] if r['id'] in out['selected_ids']}
            truth = set(case['relevant'])
            scout_rows.append({'id': case['id'], 'relevant': sorted(truth),
                               'baseline_exhaustive_eligible': sorted(eligible), 'shortlist': sorted(selected),
                               'missed_by_shortlist': sorted(truth - selected),
                               'unknown_or_excluded': sorted(truth - eligible),
                               'baseline_hits': len(truth & eligible), 'shortlist_hits': len(truth & selected),
                               'truth_count': len(truth), 'absence_proven': out['absence_proven']})
        for case in data['failures']:
            result = test_triage.classify({'error': case['error'], 'exit_code': 7})
            failure_rows.append({'id': case['id'], 'truth': case['truth'],
                                 'prediction': result['category'], 'baseline': 'unknown',
                                 'failure_preserved': result['exit_code'] == 7 and result['test_failed'],
                                 'correct': result['category'] == case['truth']})
        # Exercise the actual executable path against the same public disposable source.
        cli = subprocess.run([sys.executable, '-m', 'jevkit', 'triage', '--preset', 'test-failure'],
                             cwd=REPO, input=json.dumps([{'error': 'AssertionError', 'exit_code': 7}]),
                             text=True, capture_output=True, timeout=10)
        cli_body = json.loads(cli.stdout)
        assert cli.returncode == 1 and cli_body['results'][0]['exit_code'] == 7
    relevant = sum(r['truth_count'] for r in scout_rows)
    unknowns = [r for r in failure_rows if r['truth'] == 'unknown']
    knowns = [r for r in failure_rows if r['truth'] != 'unknown']
    return {'measurement': 'offline_synthetic_deterministic_only',
            'fixture_sha256': hashlib.sha256(fixture.read_bytes()).hexdigest(),
            'protocol': data['protocol'], 'scouting_rows': scout_rows, 'failure_rows': failure_rows,
            'scouting': {'tasks': len(scout_rows), 'relevant_files': relevant,
                         'exhaustive_eligible_recall': sum(r['baseline_hits'] for r in scout_rows) / relevant,
                         'shortlist_recall': sum(r['shortlist_hits'] for r in scout_rows) / relevant,
                         'all_missed_recoverable_or_unknown': all(not r['absence_proven'] for r in scout_rows)},
            'failure_triage': {'rows': len(failure_rows),
                               'accuracy': sum(r['correct'] for r in failure_rows) / len(failure_rows),
                               'abstain_baseline_accuracy': len(unknowns) / len(failure_rows),
                               'known_coverage': sum(r['prediction'] != 'unknown' for r in knowns) / len(knowns),
                               'false_certainty_on_unknown': sum(r['prediction'] != 'unknown' for r in unknowns) / len(unknowns),
                               'all_failures_preserved': all(r['failure_preserved'] for r in failure_rows)},
            'cli_smoke': {'exit_code': cli.returncode, 'original_failure_exit': 7},
            'live': {'measured': False, 'accuracy': None, 'latency_ms': None, 'cost_usd': None,
                     'savings': None, 'gate': 'exact public dataset and spend approval required'},
            'mocked_model_accuracy': None, 'promotion': 'NOT_PROMOTED'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture', type=Path, default=REPO / 'evals/file-evidence/transfer-v1.json')
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    report = evaluate(args.fixture)
    text = json.dumps(report, indent=2) + '\n'
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text)
    print(text)


if __name__ == '__main__':
    main()
