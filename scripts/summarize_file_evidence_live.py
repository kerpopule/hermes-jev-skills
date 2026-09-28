#!/usr/bin/env python3
"""Recompute pilot metrics from retained live receipts; no provider requests."""
import json
import math
from pathlib import Path
import sys


def percentile(values, p):
    return sorted(values)[math.ceil(len(values) * p) - 1] if values else None


def ratio(a, b):
    return a / b if b else None


def summarize(rows, receipts):
    scouts = [r for r in rows if r['kind'] == 'scouting']
    failures = [r for r in rows if r['kind'] == 'failure']
    report = {'scouting_tasks': len(scouts), 'failure_records': len(failures), 'scouting': {}, 'failure': {}}
    for mode in ('baseline', 'semantic', 'enhanced'):
        wanted = sum(len(r['gold']) for r in scouts)
        hits = sum(len(set(r['gold']) & set(r[mode])) for r in scouts)
        selected = sum(len(r[mode]) for r in scouts)
        report['scouting'][mode] = {'recall': ratio(hits, wanted), 'precision': ratio(hits, selected),
            'relevant_hits': hits, 'relevant_total': wanted, 'selected_files': selected,
            'mean_shortlist_size': ratio(selected, len(scouts)),
            'missed_ids': [r['id'] + ':' + str(i) for r in scouts for i in r['gold'] if i not in r[mode]],
            'selected_context_bytes': sum(sum(r['excerpt_bytes'][i] for i in r[mode]) for r in scouts),
            'selected_context_heuristic_tokens': sum(sum(math.ceil(r['excerpt_bytes'][i]/4) for i in r[mode]) for r in scouts)}
    report['scouting']['exhaustive_eligible_recall'] = ratio(sum(len(set(r['gold']) & set(r['eligible'])) for r in scouts), sum(len(r['gold']) for r in scouts))
    report['scouting']['all_context_bytes'] = sum(sum(r['excerpt_bytes']) for r in scouts)
    report['scouting']['all_context_heuristic_tokens'] = sum(sum(math.ceil(b/4) for b in r['excerpt_bytes']) for r in scouts)
    report['scouting']['recoverable'] = all(r['all_evidence_recoverable'] and not r['absence_proven'] for r in scouts)
    scout_tokens = sum(d.get('input_tokens') or 0 for r in scouts for d in r['advice'])
    selected_tokens = report['scouting']['enhanced']['selected_context_heuristic_tokens']
    all_tokens = report['scouting']['all_context_heuristic_tokens']
    report['scouting']['context_accounting'] = {
        'provider_reported_input_tokens': scout_tokens,
        'frontier_excerpt_tokens_heuristic': selected_tokens,
        'enhanced_combined_tokens_mixed_measured_and_estimated': scout_tokens + selected_tokens,
        'exhaustive_excerpt_tokens_heuristic': all_tokens,
        'net_tokens_saved_heuristic': all_tokens - selected_tokens - scout_tokens,
        'caveat': 'Excerpt text only; ceil UTF-8 bytes divided by four is not a tokenizer. Excludes reference and response overhead. Provider tokens are reported usage, not this heuristic.'}
    for mode in ('baseline', 'enhanced'):
        known = [r for r in failures if r['gold'] != 'unknown']
        unknown = [r for r in failures if r['gold'] == 'unknown']
        misleading = [r for r in failures if r['misleading']]
        report['failure'][mode] = {
            'accuracy': ratio(sum(r[mode]['category'] == r['gold'] for r in failures), len(failures)),
            'known_coverage': ratio(sum(r[mode]['category'] != 'unknown' for r in known), len(known)),
            'known_correct_coverage': ratio(sum(r[mode]['category'] == r['gold'] for r in known), len(known)),
            'unknown_false_certainty': ratio(sum(r[mode]['category'] != 'unknown' for r in unknown), len(unknown)),
            'misleading_false_certainty': ratio(sum(r[mode]['category'] != 'unknown' for r in misleading), len(misleading)),
            'abstentions': sum(r[mode]['category'] == 'unknown' for r in failures),
            'errors': [{'id': r['id'], 'gold': r['gold'], 'prediction': r[mode]['category']} for r in failures if r[mode]['category'] != r['gold']],
            'failure_preserved': all(r[mode]['exit_code'] == 7 and r[mode]['test_failed'] and not r[mode]['proven_root_cause'] for r in failures)}
        for kind, data in [('scouting', scouts), ('failure', failures)]:
            report[kind][mode]['p50_ms'] = percentile([r[mode + '_ms'] for r in data], .5)
            report[kind][mode]['p95_ms'] = percentile([r[mode + '_ms'] for r in data], .95)
    reservations = [r for r in receipts if r['kind'] == 'reservation']
    responses = [r for r in receipts if r['kind'] == 'response']
    decisions = [d for r in scouts for d in r['advice']] + [r['enhanced']['advice'] for r in failures if 'advice' in r['enhanced']]
    report['usage'] = {'logical_provider_attempts': len(reservations), 'responses': len(responses),
        'provider_input_tokens': sum(r.get('provider_input_tokens') or 0 for r in responses),
        'responses_missing_usage': sum(r.get('provider_input_tokens') is None for r in responses),
        'wire_bytes': sum(r['wire_bytes'] for r in reservations),
        'wire_heuristic_tokens': sum(r['heuristic_wire_tokens'] for r in reservations),
        'list_usd_estimate': sum(r.get('list_usd_estimate') or 0 for r in responses),
        'actual_billing_verified': False, 'models': sorted(set(r['model'] for r in responses if r.get('model'))),
        'p50_ms': percentile([r['elapsed_ms'] for r in responses], .5),
        'p95_ms': percentile([r['elapsed_ms'] for r in responses], .95),
        'decision_errors': [d.get('error') or d.get('reason') for d in decisions if d.get('source') != 'jev']}
    report['usage']['scouting_provider_input_tokens'] = scout_tokens
    report['usage']['failure_provider_input_tokens'] = report['usage']['provider_input_tokens'] - scout_tokens
    report['promotion'] = 'NOT_PROMOTED: synthetic pilot below frozen sample minimum; actual billing unavailable; inspect recall, robustness, latency and net cost'
    return report


def main():
    folder = Path(__file__).resolve().parents[1] / 'evals/file-evidence/live-results'
    for path in sorted(folder.glob('*-rows.jsonl')):
        tag = path.name[:-len('-rows.jsonl')]
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        receipts = [json.loads(line) for line in (folder / (tag + '-requests.jsonl')).read_text().splitlines()]
        report = summarize(rows, receipts)
        (folder / (tag + '-summary.json')).write_text(json.dumps(report, indent=2) + '\n')
        print(tag, json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
