#!/usr/bin/env python3
"""Hash-only offline screening preflight. Never performs inference or reads keys.

Run standalone, not inside a serving process: the scoped client.ask replacement
captures planned state/questions and raises an unexecuted marker, never a
fabricated model answer. This is neither a paid collector nor a detection score.
"""
import argparse
import csv
import io
import json
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from jevkit import client, rerank, webscreen
from webscreen_heldout import digest, freeze, sha


def prepare(test_bytes, reserve_bytes, manifest):
    actual = freeze(test_bytes, reserve_bytes)
    if actual != manifest:
        raise ValueError('source or selection differs from frozen manifest')
    source_rows = list(csv.DictReader(io.StringIO(test_bytes.decode('utf-8-sig'), newline='')))
    rows = []
    for row in manifest['test']:
        captures = []
        def capture(state, questions, **kwargs):
            payload = {'state': state, 'questions': questions}
            captures.append({'screening_payload_sha256': digest(payload),
                             'payload_utf8_bytes': len(json.dumps(payload, sort_keys=True,
                                separators=(',', ':'), ensure_ascii=True).encode('utf-8')),
                             'questions': len(questions)})
            raise client.JevError('evaluation_unexecuted')
        text = source_rows[row['source_index']]['text']
        if sha(text.encode('utf-8')) != row['text_sha256']:
            raise ValueError('row source mismatch')
        # Exactly the real web_extract-content splitting/redaction/question path.
        # Discard its local-only/fail-open verdict: it is NOT a model observation.
        with patch.object(client, 'ask', new=capture):
            webscreen.screen('web_extract', json.dumps({'results': [{'content': text}]}))
        rows.append({'id': row['id'], 'requests': sorted(captures,
                     key=lambda entry: entry['screening_payload_sha256'])})
    files = ['webscreen.py', 'rerank.py', 'privacy.py', 'client.py']
    return {
        'schema': 1, 'mode': 'offline-unexecuted-screening-preflight',
        'manifest_sha256': digest(manifest), 'source_sha256': actual['source_sha256'],
        'detector_files_sha256': {name: sha((ROOT / 'jevkit' / name).read_bytes()) for name in files},
        'question_template_sha256': sha(rerank.injection_question('{label}').encode('utf-8')),
        'threshold': webscreen.INJECTION_THRESHOLD,
        'input_shape': 'single dataset row as web_extract content',
        'test_rows': len(rows), 'reserve_rows': len(manifest['reserve']),
        'planned_requests': sum(len(row['requests']) for row in rows),
        'rows_without_requests': sum(not row['requests'] for row in rows),
        'inference_calls_executed': 0, 'rows': rows,
        'limitations': [
            'No inference, credential lookup, detection score or paid cost estimate.',
            'Reserve source identities verified; reserve content never screened.',
            'Payload hashes omit provider/model fields; full wire hashes require an authorized collector.',
            'Client retries, pricing and charges require an explicit separately approved run budget.',
            'Sensitive/local-only rows remain in denominators; no fabricated model outputs.',
            'Dataset labels are not adjudicated web-injection ground truth.',
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--test', type=Path, required=True)
    parser.add_argument('--reserve', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, default=ROOT / 'evals/web-screen/heldout-manifest-v1.json')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    plan = prepare(args.test.read_bytes(), args.reserve.read_bytes(),
                   json.loads(args.manifest.read_text(encoding='utf-8')))
    # Validate everything before creating a receipt; never overwrite existing output.
    with args.output.open('x', encoding='utf-8') as output:
        output.write(json.dumps(plan, indent=2, sort_keys=True, allow_nan=False) + '\n')
    print(json.dumps({key: plan[key] for key in ('mode', 'manifest_sha256', 'test_rows',
                       'reserve_rows', 'planned_requests', 'rows_without_requests', 'inference_calls_executed')}))


if __name__ == '__main__':
    main()
