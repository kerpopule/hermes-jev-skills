#!/usr/bin/env python3
"""Recount frozen independent-v2 receipts offline. No provider calls."""
import collections
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'evals/file-evidence/independent-v2'


def percentile(values, quantile):
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * quantile) - 1)] if ordered else None


def latency(values):
    return {'n': len(values), 'p50_ms': percentile(values, .5), 'p95_ms': percentile(values, .95)}


def summarize(folder):
    rows = [json.loads(line) for path in sorted(folder.glob('*-rows.jsonl')) for line in path.read_text().splitlines()]
    failures = [r for r in rows if r['kind'] == 'failure']
    scouts = [r for r in rows if r['kind'] == 'scouting']
    if len(failures) != 64 or len(scouts) != 8 or len({r['id'] for r in failures + scouts}) != 72:
        raise ValueError('Incomplete or duplicate independent validation cases')
    modes = collections.defaultdict(list)
    for path in sorted(folder.glob('*-requests.jsonl')):
        events = [json.loads(line) for line in path.read_text().splitlines()]
        contexts = {r['seq']: r for r in events if r['kind'] == 'context'}
        reservations = {r['seq']: r for r in events if r['kind'] == 'reservation'}
        responses = {r['seq']: r for r in events if r['kind'] == 'response'}
        for kind in ['context', 'reservation', 'response']:
            sequence = [r['seq'] for r in events if r['kind'] == kind]
            if len(sequence) != len(set(sequence)):
                raise ValueError('Duplicate receipt sequence in ' + path.name)
        if set(contexts) != set(reservations) or set(responses) - set(reservations):
            raise ValueError('Unmatched receipt context/response in ' + path.name)
        for seq, request in reservations.items():
            modes[contexts[seq]['mode']].append({**request, **responses.get(seq, {})})
    def accounting(requests):
        known = [r for r in requests if r.get('provider_input_tokens') is not None]
        return {'requests': len(requests), 'usage_known': len(known), 'usage_missing': len(requests)-len(known),
                'reported_input_tokens': sum(r['provider_input_tokens'] for r in known),
                'reported_list_usd_estimate': sum(r.get('list_usd_estimate') or 0 for r in known),
                'wire_bytes': sum(r['wire_bytes'] for r in requests),
                'latency': latency([r['elapsed_ms'] for r in requests if 'elapsed_ms' in r])}
    result = {'evaluated_cases': 72, 'scouting_cases': 8, 'failure_cases': 64,
              'provider': {k: accounting(v) for k, v in modes.items()},
              'provider_total': accounting([r for values in modes.values() for r in values]),
              'triage': {}, 'scouting': {}}
    for mode in ['baseline', 'extended', 'enhanced', 'extended_enhanced']:
        predictions = [(r, r[mode]['category'] if isinstance(r[mode], dict) else r[mode]) for r in failures]
        accepted = [(r, p) for r, p in predictions if p != 'unknown']
        result['triage'][mode] = {
            'correct': sum(p == r['gold'] for r, p in predictions), 'n': len(predictions),
            'abstained': sum(p == 'unknown' for r, p in predictions),
            'known_gold': sum(r['gold'] != 'unknown' for r, p in predictions),
            'known_correct': sum(p == r['gold'] and r['gold'] != 'unknown' for r, p in predictions),
            'unknown_gold': sum(r['gold'] == 'unknown' for r, p in predictions),
            'unknown_correct': sum(p == r['gold'] == 'unknown' for r, p in predictions),
            'accepted': len(accepted), 'accepted_correct': sum(p == r['gold'] for r, p in accepted),
            'by_provenance': {group: {'n': sum(r['id'].startswith('executed-') == executed for r, p in predictions), 'correct': sum(r['id'].startswith('executed-') == executed and p == r['gold'] for r, p in predictions)} for group, executed in [('actual_python_execution', True), ('authored_diagnostic', False)]},
            'errors': [{'id': r['id'], 'gold': r['gold'], 'predicted': p} for r, p in predictions if p != r['gold']],
            'latency': latency([r[mode + '_ms'] for r in failures]) if mode != 'extended_enhanced' else None}
    for mode in scouts[0]['sets']:
        selected = sum(len(r['sets'][mode]) for r in scouts)
        hits = sum(len(set(r['gold']) & set(r['sets'][mode])) for r in scouts)
        gold = sum(len(r['gold']) for r in scouts)
        compact = sum(r['contexts_compact'][mode]['frontier_tokens_estimate'] for r in scouts)
        verbose = sum(r['contexts'][mode]['frontier_tokens_estimate'] for r in scouts)
        bm = sum(r['contexts_compact']['bm25_8']['frontier_tokens_estimate'] for r in scouts)
        direct = sum(math.ceil(r['direct_excerpt_bytes'] / 4) for r in scouts)
        full = sum(math.ceil(r['full_manifest_bytes'] / 4) for r in scouts)
        exhaustive = sum(r['contexts_compact']['exhaustive']['frontier_tokens_estimate'] for r in scouts)
        provider_mode = 'single' if mode in {'semantic_only', 'enhanced_single'} else 'batch' if mode in {'batch_only', 'enhanced_batch'} else None
        provider = result['provider'].get(provider_mode, {})
        jev = provider.get('reported_input_tokens', 0)
        cost = provider.get('reported_list_usd_estimate', 0)
        saving = bm - compact
        result['scouting'][mode] = {
            'critical_complete_tasks': sum(set(r['critical']) <= set(r['sets'][mode]) for r in scouts),
            'critical_misses': [{'id': r['id'], 'missing': sorted(set(r['critical']) - set(r['sets'][mode]))} for r in scouts if not set(r['critical']) <= set(r['sets'][mode])],
            'selected': selected, 'relevant_selected': hits, 'gold': gold,
            'precision': hits / selected if selected else None, 'recall': hits / gold,
            'compact_frontier_tokens_estimate': compact, 'verbose_frontier_tokens_estimate': verbose,
            'optimistic_direct_read_tokens_estimate': direct,
            'full_source_direct_read_tokens_estimate': full,
            'frontier_saved_vs_exhaustive': exhaustive-compact,
            'net_input_tokens_saved_vs_exhaustive': exhaustive-compact-jev,
            'break_even_frontier_usd_per_million_vs_exhaustive': cost * 1000000 / (exhaustive-compact) if exhaustive > compact and provider_mode else None,
            'frontier_saved_vs_bm25_8': saving, 'frontier_saved_vs_direct': direct-compact,
            'provider_input_tokens': jev, 'net_input_tokens_saved_vs_bm25_8': saving-jev,
            'break_even_frontier_usd_per_million_vs_bm25_8': cost * 1000000 / saving if saving > 0 and provider_mode else None}
    result['coverage'] = {'recoverable_tasks': sum(r['all_eligible_recovered'] for r in scouts),
                          'eligible_per_task': [len(r['eligible']) for r in scouts],
                          'unknown': {r['id']: r['unknown'] for r in scouts},
                          'advice_actions': {mode: dict(collections.Counter(a['action'] for r in scouts for a in r[mode].values())) for mode in ['single_advice', 'batch_advice']}}
    corpus = json.loads((DATA / 'corpus.json').read_text())
    full_text = {r['id']: r['text'] for r in corpus['files']}
    result['coverage']['truncated_excerpt_observations'] = sum(len(v['text'].splitlines()) < len(full_text[k].splitlines()) for r in scouts for k, v in r['recovered'].items())
    result['coverage']['excerpt_observations'] = sum(len(r['recovered']) for r in scouts)
    result['coverage']['withheld_reasons'] = {mode: dict(collections.Counter(a.get('reason', a.get('error')) or 'none' for r in scouts for a in r[mode].values() if not a.get('sent_to_jev'))) for mode in ['single_advice', 'batch_advice']}
    result['triage_exit_preservation'] = all(r['baseline']['exit_code'] == r['enhanced']['exit_code'] == next(c['exit_code'] for c in corpus['failures'] if c['id'] == r['id']) for r in failures)
    result['scouting_latency'] = {mode: latency([r[mode] for r in scouts]) for mode in ['local_ms', 'bm25_ms', 'enhanced_single_ms', 'enhanced_batch_ms']}
    result['limitations'] = [
        'Frontier tokens are ceil(UTF-8 JSON bytes/4), not provider tokenizer counts.',
        'Direct-read lower bound excludes prompt, query, references, and warnings.',
        'Batch and single are sequential, not randomized crossover timing.',
        'Extended+Jev reuses advice; no independent latency sample exists.',
        'Input-token totals do not imply monetary equivalence between providers.',
        'List estimates are not invoices. Missing usage is not zero cost.',
        'Raw HTTP response bodies were not retained; normalized advice and accounting survive.',
        'This corpus is now observed, not fresh validation for future tuning.']
    return result


if __name__ == '__main__':
    print(json.dumps(summarize(DATA / 'results'), indent=2, sort_keys=True))
