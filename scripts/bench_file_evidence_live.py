#!/usr/bin/env python3
"""Bounded, opt-in synthetic live pilot. No credentials or raw responses are logged."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from jevkit import client, file_evidence as ev, keystore, ledger, limits, test_triage

CORPUS = ROOT / 'evals/file-evidence/live-v1-corpus.json'
PROTOCOL = ROOT / 'evals/file-evidence/live-v1-protocol.md'
RESULTS = ROOT / 'evals/file-evidence/live-results'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def append(path, row):
    with path.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(row, sort_keys=True) + '\n')
        handle.flush()
        os.fsync(handle.fileno())


def load_rows(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


class Guard:
    def __init__(self, receipts):
        self.receipts = receipts
        self.started = time.monotonic()
        self.count = 0
        self.failures = 0
        self.existing = sum(sum(r.get('kind') == 'reservation' for r in load_rows(p))
                            for p in RESULTS.glob('*-requests.jsonl'))

    def __call__(self, body, headers, timeout):
        if (self.count >= 100 or self.existing + self.count >= 600
                or time.monotonic() - self.started >= 600 or self.failures >= 5):
            raise RuntimeError('pilot_guard_stop')
        if len(body) > 16000:
            raise RuntimeError('pilot_wire_size_stop')
        self.count += 1
        seq = self.count
        # Persist reservation BEFORE network, so even a crash leaves a counted attempt.
        append(self.receipts, {'kind': 'reservation', 'seq': seq,
                              'wire_bytes': len(body), 'wire_sha256': hashlib.sha256(body).hexdigest(),
                              'heuristic_wire_tokens': math.ceil(len(body) / 4),
                              'reserved_list_usd_estimate': 0.002})
        start = time.monotonic()
        try:
            raw = client._http_transport(body, headers, timeout)
        except client.JevError as error:
            self.failures += 1
            append(self.receipts, {'kind': 'response', 'seq': seq, 'error': error.code,
                                  'elapsed_ms': (time.monotonic() - start) * 1000})
            raise
        try:
            data = json.loads(raw)
            usage = data.get('usage', {}) if isinstance(data, dict) else {}
            tokens = usage.get('input_tokens') if isinstance(usage, dict) else None
            if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0:
                tokens = None
            model = data.get('model') if isinstance(data, dict) else None
            # Only numerical accounting and bounded model metadata, never arbitrary response text.
            model = model if isinstance(model, str) and model.startswith('jev-') and len(model) < 80 else None
            append(self.receipts, {'kind': 'response', 'seq': seq, 'provider_input_tokens': tokens,
                                  'model': model, 'elapsed_ms': (time.monotonic() - start) * 1000,
                                  'list_usd_estimate': ledger.cost(tokens)['list_usd'] if tokens is not None else None})
        except (ValueError, TypeError):
            append(self.receipts, {'kind': 'response', 'seq': seq, 'error': 'malformed',
                                  'elapsed_ms': (time.monotonic() - start) * 1000})
        return raw

    def observe(self, result):
        if result.get('source') == 'fallback':
            self.failures += 1
        elif result.get('source') == 'jev':
            self.failures = 0
        if result.get('error') in {'no_key', 'http_401', 'http_403'} or self.failures >= 5:
            raise RuntimeError('pilot_provider_stop')


def timed(fn):
    start = time.monotonic()
    result = fn()
    return result, (time.monotonic() - start) * 1000


def run(panel, tag):
    if keystore.provider() != 'typesafe':
        raise SystemExit('pilot requires configured TypeSafe; no provider substitution')
    if os.environ.get('TYPESAFE_BASE_URL', '').rstrip('/') not in ('', 'https://api.typesafe.ai'):
        raise SystemExit('custom endpoints not permitted for this pilot')
    if os.environ.get('TYPESAFE_MODEL') not in (None, '', 'jev-latest', 'jev-1.13.0'):
        raise SystemExit('unreviewed model override')
    if limits.disabled():
        raise SystemExit('existing global limiter must remain enabled')
    expected = json.loads((CORPUS.parent / 'live-v1-freeze.json').read_text())
    if expected != {'corpus_sha256': digest(CORPUS), 'protocol_sha256': digest(PROTOCOL)}:
        raise SystemExit('frozen corpus or protocol changed')
    if not tag.replace('-', '').isalnum() or len(tag) > 48:
        raise SystemExit('invalid tag')
    RESULTS.mkdir(exist_ok=True)
    output = RESULTS / (tag + '-rows.jsonl')
    receipts = RESULTS / (tag + '-requests.jsonl')
    if output.exists() or receipts.exists():
        raise SystemExit('tag already exists; retain receipts and use an explicit new run tag')
    guard = Guard(receipts)
    cases = json.loads(CORPUS.read_text())['panels'][panel]
    append(output, {'kind': 'manifest', 'panel': panel, **expected,
                    'git_revision': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                    'source_sha256': {p: digest(ROOT / p) for p in ['jevkit/file_evidence.py', 'jevkit/test_triage.py',
                                      'jevkit/policies/file-relevance.json', 'jevkit/policies/failure-symptom.json']}})
    for task in cases['scouting']:
        with tempfile.TemporaryDirectory(prefix='jev-live-') as folder:
            root = Path(folder).resolve()
            paths = []
            for i, text in enumerate(task['texts']):
                name = 'excerpt%d.txt' % i
                (root / name).write_text(text)
                paths.append(name)
            base, base_ms = timed(lambda: ev.scout(str(root), paths, task['query'], top_k=1))
            refs = {ref['path']: ref for ref in base['evidence']}
            baseline = [i for i, path in enumerate(paths) if refs.get(path, {}).get('id') in base['selected_ids']]
            advice = []
            recovery_start = time.monotonic()
            for i, path in enumerate(paths):
                if path not in refs:
                    advice.append({'action': 'unknown', 'sent_to_jev': False, 'reason': 'ineligible'})
                    continue
                excerpt = ev.recover(str(root), refs[path])['text']
                result = ev.public_advice(excerpt, purpose='file-relevance', query=task['query'],
                                          approved_public=True, transport=guard)
                advice.append(result)
                guard.observe(result)
            extra_ms = (time.monotonic() - recovery_start) * 1000
            semantic = [i for i, r in enumerate(advice) if r['action'] == 'relevant']
            enhanced = sorted(set(baseline + semantic))
            append(output, {'kind': 'scouting', 'id': task['id'], 'gold': task['relevant'],
                            'baseline': baseline, 'semantic': semantic, 'enhanced': enhanced,
                            'eligible': [i for i,p in enumerate(paths) if p in refs], 'advice': advice,
                            'baseline_ms': base_ms, 'enhanced_ms': base_ms + extra_ms,
                            'excerpt_bytes': [len(text.encode()) for text in task['texts']],
                            'all_evidence_recoverable': len(refs) == len(paths), 'absence_proven': base['absence_proven']})
    for task in cases['failures']:
        state = {'error': task['text'], 'exit_code': 7}
        base, base_ms = timed(lambda: test_triage.classify(state))
        enhanced, enhanced_ms = timed(lambda: test_triage.classify({**state, 'semantic': True, 'approved_public': True}, transport=guard))
        append(output, {'kind': 'failure', 'id': task['id'], 'gold': task['label'],
                        'misleading': task.get('misleading', False), 'baseline': base, 'enhanced': enhanced,
                        'baseline_ms': base_ms, 'enhanced_ms': enhanced_ms})
        guard.observe(enhanced.get('advice', {}))
    print(json.dumps({'rows': str(output.relative_to(ROOT)), 'requests': guard.count, 'panel': panel}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--freeze', action='store_true')
    parser.add_argument('--panel', choices=['A', 'B'])
    parser.add_argument('--tag')
    parser.add_argument('--live-approved-public-synthetic', action='store_true')
    args = parser.parse_args()
    if args.freeze:
        target = CORPUS.parent / 'live-v1-freeze.json'
        with target.open('x') as handle:
            json.dump({'corpus_sha256': digest(CORPUS), 'protocol_sha256': digest(PROTOCOL)}, handle, indent=2)
        print(target.relative_to(ROOT))
    elif args.live_approved_public_synthetic and args.panel and args.tag:
        run(args.panel, args.tag)
    else:
        parser.error('freeze first; live run requires exact panel, unique tag and explicit approval flag')


if __name__ == '__main__':
    main()
