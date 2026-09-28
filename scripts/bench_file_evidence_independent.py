#!/usr/bin/env python3
"""Explicit bounded v2 PUBLIC corpus evaluation. No production activation.

Run --scout-index N per task, or --failures, each with a unique --tag and
--live-approved-public-synthetic. The frozen protocol governs interpretation.
"""
import argparse
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))
from jevkit import decide, file_evidence as ev, keystore, limits, policy, test_triage
import bench_file_evidence_live as v1
from file_evidence_comparators import bm25, extended_failure

DATA = ROOT / 'evals/file-evidence/independent-v2'
RESULTS = DATA / 'results'


def verify_freeze():
    freeze = json.loads((DATA / 'freeze.json').read_text())
    for name, wanted in freeze['sha256'].items():
        if v1.digest(ROOT / name) != wanted:
            raise ValueError('frozen input changed: ' + name)
    return freeze


def eligible(text, query):
    # Exact same screens as public_advice, no redaction to rescue corpus cases.
    return (len(text) <= 3000 and len(query) <= ev.MAX_QUERY and not ev._screen(text + '\n' + query)
            and not re.search(r'(?:https?://|[/\\]|\b[A-Za-z]:)', text + query))


def batch_advice(texts, query, transport):
    """Benchmark-only eight-question composition over decide, unchanged final rules."""
    if len(texts) > 8:
        raise ValueError('batch exceeds eight')
    out = [{'action': 'unknown', 'source': 'code', 'sent_to_jev': False,
            'reason': 'withheld_locally'} for _ in texts]
    allowed = [(i, text) for i, text in enumerate(texts) if eligible(text, query)]
    if not allowed:
        return out, None
    shipped = policy.load(str(policy.SHIPPED / 'file-relevance.json'))
    state, questions = {'query': query}, {}
    for ordinal, (_, text) in enumerate(allowed):
        key = 'D%d' % ordinal
        state[key] = text
        question = copy.deepcopy(shipped['questions']['relevance'])
        question['instructions'] += ' Judge ONLY excerpt field %s against query; other excerpt fields are not evidence for this question.' % key
        questions[key] = question
    rules = {'name': 'file-evidence-batch-experiment', 'version': 1,
             'feature': 'file_evidence_validation', 'tuned_on': shipped['tuned_on'],
             'questions': questions, 'state_fields': list(state),
             'field_limits': {key: 500 if key == 'query' else 3000 for key in state},
             'rules': [], 'otherwise': 'unknown', 'on_error': 'unknown', 'on_drift': 'unknown'}
    result = decide.decide(state, rules, timeout=4.0, retries=0, record=False, transport=transport)
    for ordinal, (index, _) in enumerate(allowed):
        reading = result.get('answers', {}).get('D%d' % ordinal)
        action = 'unknown'
        if result.get('source') == 'jev' and result.get('drift') is False and reading is not None:
            action = policy.apply(shipped, {'relevance': reading})['action']
        out[index] = {'action': action, 'source': result['source'],
                      'sent_to_jev': result['sent_to_jev'], 'reading': reading,
                      'error': result.get('error'), 'advisory_only': True}
    return out, result


class Guard(v1.Guard):
    def __init__(self, receipts):
        v1.RESULTS = RESULTS
        super().__init__(receipts)
        self.context = {}

    def __call__(self, body, headers, timeout):
        if self.existing + self.count >= 400:
            raise RuntimeError('v2_total_budget_stop')
        v1.append(self.receipts, {'kind': 'context', 'seq': self.count + 1, **self.context})
        return super().__call__(body, headers, timeout)


def delivery(query, scout, recovered, selected, advice=None):
    # Measure actual JSON envelope, including recoverable references and warnings.
    payload = {'query': query, 'evidence': scout['evidence'], 'unknown': scout['unknown'],
               'absence_proven': False, 'note': scout['note'],
               'selected': [recovered[key] for key in sorted(selected)], 'advice': advice or []}
    size = len(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode())
    return {'json_bytes': size, 'frontier_tokens_estimate': math.ceil(size / 4)}


def scout_case(task, files, guard):
    with tempfile.TemporaryDirectory(prefix='jev-independent-') as folder:
        root = Path(folder).resolve()
        for row in files:
            (root / row['path']).write_text(row['text'])
        result, local_ms = v1.timed(lambda: ev.scout(str(root), [r['path'] for r in files], task['query'], top_k=16))
        bypath = {r['path']: r for r in files}
        byid = {r['id']: bypath[r['path']]['id'] for r in result['evidence']}
        recovered = {}
        for ref in result['evidence']:
            recovered[byid[ref['id']]] = {'reference': ref, 'text': ev.recover(str(root), ref)['text']}
        eligible_files = [r for r in files if r['id'] in recovered]
        bm8, bm8_ms = v1.timed(lambda: bm25(eligible_files, task['query'], 8))
        bm16 = bm25(eligible_files, task['query'], 16)
        local = [byid[key] for key in result['selected_ids']]
        ordered = sorted(recovered)
        single, batched = {}, {}
        start = time.monotonic()
        for key in ordered:
            guard.context = {'case': task['id'], 'mode': 'single', 'items': [key]}
            single[key] = ev.public_advice(recovered[key]['text'], purpose='file-relevance',
                                          query=task['query'], approved_public=True, transport=guard)
            guard.observe(single[key])
        single_ms = (time.monotonic() - start) * 1000
        start = time.monotonic()
        for offset in range(0, len(ordered), 8):
            keys = ordered[offset:offset + 8]
            guard.context = {'case': task['id'], 'mode': 'batch', 'items': keys}
            advice, observation = batch_advice([recovered[key]['text'] for key in keys], task['query'], guard)
            batched.update(zip(keys, advice))
            if observation is not None:
                guard.observe(observation)
        batch_ms = (time.monotonic() - start) * 1000
        semantic = [key for key, value in single.items() if value['action'] == 'relevant']
        batch_selected = [key for key, value in batched.items() if value['action'] == 'relevant']
        sets = {'exhaustive': ordered, 'lexical8': local[:8], 'lexical16': local,
                'bm25_8': bm8, 'bm25_16': bm16, 'semantic_only': semantic,
                'batch_only': batch_selected, 'enhanced_single': sorted(set(bm8 + semantic)),
                'enhanced_batch': sorted(set(bm8 + batch_selected))}
        contexts = {name: delivery(task['query'], result, recovered, selected,
                                   list(single.values()) if name == 'enhanced_single' else
                                   list(batched.values()) if name == 'enhanced_batch' else None)
                    for name, selected in sets.items()}
        contexts_compact = {name: delivery(
            task['query'], result, recovered, selected,
            [{'id': key, 'action': value['action'], 'reason': value.get('reason', value.get('error'))}
             for key, value in (single if name == 'enhanced_single' else batched).items()]
            if name in {'enhanced_single', 'enhanced_batch'} else None)
            for name, selected in sets.items()}
        return {'kind': 'scouting', 'id': task['id'], 'gold': task['relevant'], 'critical': task['critical'],
                'sets': sets, 'single_advice': single, 'batch_advice': batched,
                'eligible': ordered, 'unknown': result['unknown'], 'recovered': recovered,
                'all_eligible_recovered': len(recovered) == len(result['evidence']),
                'contexts': contexts, 'contexts_compact': contexts_compact,
                'local_ms': local_ms, 'bm25_ms': bm8_ms,
                'enhanced_single_ms': local_ms + bm8_ms + single_ms,
                'enhanced_batch_ms': local_ms + bm8_ms + batch_ms,
                'direct_excerpt_bytes': sum(len(r['text'].encode()) for r in recovered.values()),
                'full_manifest_bytes': sum(len(r['text'].encode()) for r in files)}


def failure_case(task, guard):
    state = {'error': task['text'], 'exit_code': task['exit_code']}
    base, base_ms = v1.timed(lambda: test_triage.classify(state))
    extended, extra_ms = v1.timed(lambda: extended_failure(task['text'], base))
    guard.context = {'case': task['id'], 'mode': 'triage', 'items': [task['id']]}
    enhanced, enhanced_ms = v1.timed(lambda: test_triage.classify(
        {**state, 'semantic': True, 'approved_public': True}, transport=guard))
    guard.observe(enhanced.get('advice', {}))
    # Reuse measured advice; no invented extra provider call or latency sample.
    strong_enhanced = extended if extended != 'unknown' else enhanced['category']
    if base.get('reason') or (extended == 'unknown' and base['category'] != 'unknown'):
        strong_enhanced = extended
    return {'kind': 'failure', 'id': task['id'], 'gold': task['label'], 'baseline': base,
            'extended': extended, 'enhanced': enhanced, 'extended_enhanced': strong_enhanced,
            'baseline_ms': base_ms, 'extended_ms': base_ms + extra_ms, 'enhanced_ms': enhanced_ms,
            'extended_enhanced_latency': 'shared advice, not an independently timed request'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tag', required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--scout-index', type=int, choices=range(8))
    group.add_argument('--failures', action='store_true')
    parser.add_argument('--live-approved-public-synthetic', action='store_true')
    args = parser.parse_args()
    if not args.live_approved_public_synthetic:
        parser.error('explicit public/synthetic live approval required')
    if keystore.provider() != 'typesafe' or limits.disabled():
        raise SystemExit('requires configured TypeSafe with existing limiter enabled')
    if os.environ.get('TYPESAFE_BASE_URL', '').rstrip('/') not in ('', 'https://api.typesafe.ai'):
        raise SystemExit('custom endpoint prohibited')
    if os.environ.get('TYPESAFE_MODEL') not in (None, '', 'jev-latest', 'jev-1.13.0'):
        raise SystemExit('unreviewed model override')
    freeze = verify_freeze()
    if not args.tag.replace('-', '').isalnum() or len(args.tag) > 48:
        parser.error('invalid unique tag')
    RESULTS.mkdir(exist_ok=True)
    output, receipts = [RESULTS / (args.tag + suffix) for suffix in ('-rows.jsonl', '-requests.jsonl')]
    if output.exists() or receipts.exists():
        raise SystemExit('existing tag, refuse overwrite')
    output.touch(exist_ok=False)
    receipts.touch(exist_ok=False)
    guard = Guard(receipts)
    corpus = json.loads((DATA / 'corpus.json').read_text())
    v1.append(output, {'kind': 'manifest', 'freeze': freeze,
                      'runner_sha256': v1.digest(Path(__file__)),
                      'git_revision': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()})
    if args.failures:
        for task in corpus['failures']:
            v1.append(output, failure_case(task, guard))
    else:
        v1.append(output, scout_case(corpus['scouting'][args.scout_index], corpus['files'], guard))
    print(json.dumps({'tag': args.tag, 'requests': guard.count, 'completed': True}))


if __name__ == '__main__':
    main()
