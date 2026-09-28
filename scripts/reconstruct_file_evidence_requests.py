#!/usr/bin/env python3
"""Offline public-body reconstruction, verified against preexisting wire hashes.

Never resolve credentials, call a provider, or claim reconstruction of responses.
"""
import hashlib
import json
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))
from jevkit import client, decide, file_evidence as ev, policy, test_triage
import bench_file_evidence_independent as bench


class OfflineGuard:
    def context(self, **kwargs):
        pass

    def observe(self, result):
        pass

    def __call__(self, *args):
        raise AssertionError('No transport permitted during reconstruction')


def reconstruct():
    bench.verify_freeze()
    data = ROOT / 'evals/file-evidence/independent-v2'
    corpus = json.loads((data / 'corpus.json').read_text())
    bodies = {}

    def capture(state, rules, **kwargs):
        rules = policy.load(rules) if not isinstance(rules, dict) else rules
        prepared = decide.prepare_state(state, rules)
        questions = client.check_questions(rules['questions'])
        for model in ['jev-latest', 'jev-1.13.0']:
            body = json.dumps({'state': prepared, 'model': model, 'questions': questions}, separators=(',', ':'), default=str).encode()
            bodies[hashlib.sha256(body).hexdigest()] = body
        return {'source': 'fallback', 'sent_to_jev': False, 'action': 'unknown', 'answers': {}}

    # Patching the policy dispatcher stops before limits, credentials, ledger or network.
    with patch.object(decide, 'decide', capture), patch.object(client, '_http_transport', side_effect=AssertionError('network forbidden')):
        for path in sorted((data / 'results').glob('*-rows.jsonl')):
            for line in path.read_text().splitlines():
                row = json.loads(line)
                if row['kind'] != 'scouting':
                    continue
                task = next(t for t in corpus['scouting'] if t['id'] == row['id'])
                items = [row['recovered'][key]['text'] for key in sorted(row['recovered'])]
                for text in items:
                    ev.public_advice(text, purpose='file-relevance', query=task['query'], approved_public=True)
                for start in range(0, len(items), 8):
                    bench.batch_advice(items[start:start + 8], task['query'], OfflineGuard())
        for case in corpus['failures']:
            test_triage.classify({'error': case['text'], 'exit_code': case['exit_code'], 'semantic': True, 'approved_public': True})
    recovered = []
    for path in sorted((data / 'results').glob('*-requests.jsonl')):
        for line in path.read_text().splitlines():
            receipt = json.loads(line)
            if receipt['kind'] != 'reservation':
                continue
            body = bodies.get(receipt['wire_sha256'])
            if body is None or len(body) != receipt['wire_bytes']:
                raise ValueError('Unmatched request: %s seq %s' % (path.name, receipt['seq']))
            recovered.append({'receipt_file': path.name, 'seq': receipt['seq'], 'wire_sha256': receipt['wire_sha256'],
                              'wire_bytes': len(body), 'body_utf8': body.decode(), 'provenance': 'offline reconstruction verified against original wire SHA-256'})
    if len(recovered) != 318:
        raise ValueError('Expected exactly 318 retained reservations')
    return recovered


if __name__ == '__main__':
    for row in reconstruct():
        print(json.dumps(row, sort_keys=True))
