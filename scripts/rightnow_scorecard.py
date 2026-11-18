#!/usr/bin/env python3
"""Offline summary of retained synthetic journals. Never calls a provider.

Jev cost is an explicitly labeled configured-rate estimate, NOT an invoice.
Do not mix the nested-negation pilot with the direct-question matched cohort.
"""
from __future__ import annotations
import argparse
import json
import math
from pathlib import Path
import statistics


def summarize(paths, jev_input_rate=.042):
    groups = {}
    errors = []
    seen = set()
    for path in paths:
        rows = [json.loads(line) for line in Path(path).read_text().splitlines()]
        for row in rows:
            if 'error' in row:
                errors.append({'id': row.get('id'), 'provider': row.get('provider', 'rightnow'), 'error': row['error']})
            if 'result' not in row:
                continue
            provider = row.get('provider', 'rightnow')
            ident = row['id']
            key = (str(path), provider, ident)
            if key in seen:
                raise ValueError('duplicate result in journal')
            seen.add(key)
            cohort = ('original-negation-pilot' if Path(path).name == 'text-smoke.jsonl'
                      else 'original-negation-baseline' if ident.startswith('original-baseline-')
                      else 'media' if ident.split('-')[0] in ('image', 'pdf', 'audio', 'video')
                      else 'matched-direct')
            groups.setdefault((cohort, provider), []).append(row)
    output = []
    for (cohort, provider), rows in sorted(groups.items()):
        times = sorted(row['result']['latency_ms'] for row in rows)
        costs = [row['result'].get('usage', {}).get('cost') for row in rows]
        usage = sum(row['result'].get('usage', {}).get('input_tokens', 0) for row in rows)
        nouls = [row for row in rows if row['result']['answers']['check']['type'] == 'noul' and 'expected' in row]
        brier = statistics.mean((r['result']['answers']['check']['noul'] - int(r['expected'])) ** 2 for r in nouls) if nouls else None
        output.append({'cohort': cohort, 'provider': provider, 'n': len(rows),
                       'correct': sum(row['correct'] is True for row in rows),
                       'latency_p50_ms': statistics.median(times),
                       'latency_p95_ms': times[math.ceil(len(times) * .95) - 1],
                       'brier_noul': brier, 'input_tokens': usage,
                       'reported_cost_usd': sum(costs) if all(isinstance(c, (int, float)) for c in costs) else None,
                       'jev_configured_input_rate_estimate_usd': usage * jev_input_rate / 1e6 if provider == 'jev' else None,
                       'invoice_verified': False})
    return {'scope': 'small synthetic corpus only; not production skill/caller acceptance',
            'jev_estimate_rate_usd_per_million_input': jev_input_rate,
            'cohorts': output, 'errors': errors, 'live_calls_made_by_summary': 0}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('journals', nargs='+', type=Path)
    p.add_argument('--jev-input-rate', type=float, default=.042)
    args = p.parse_args()
    if not math.isfinite(args.jev_input_rate) or args.jev_input_rate < 0:
        p.error('rate must be finite and nonnegative')
    print(json.dumps(summarize(args.journals, args.jev_input_rate), indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
