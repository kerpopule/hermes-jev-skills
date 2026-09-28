"""Explicit evidence helpers. JSON in/out, never an automatic tool hook."""
from __future__ import annotations

import json
import sys

from . import file_evidence


def command(args):
    try:
        raw = sys.stdin.read(100000)
        if len(raw) >= 100000:
            raise ValueError('request too large')
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError('expected a JSON object')
        if args.action == 'scout':
            out = file_evidence.scout(data['root'], data['paths'], data['query'],
                                      top_k=data.get('top_k', 8))
        elif args.action == 'read':
            out = file_evidence.recover(data['root'], data['reference'])
        else:
            # The flag is deliberate, visible acknowledgement, not an inferred approval.
            out = file_evidence.public_advice(data['excerpt'], purpose=data['purpose'],
                                             query=data.get('query', ''),
                                             approved_public=args.approved_public_and_spend)
        print(json.dumps(out, indent=2))
        return 0
    except (ValueError, TypeError, KeyError, OSError, RecursionError):
        # Do not echo a malformed input, path, secret or exception text.
        print(json.dumps({'error': 'invalid_evidence_request', 'sent_to_jev': False}))
        return 2


def register(sub):
    parser = sub.add_parser('evidence', help='opt-in local file scouting and advisory public-data classification')
    parser.add_argument('action', choices=['scout', 'read', 'advise'])
    parser.add_argument('--approved-public-and-spend', action='store_true',
                        help='advise only: owner approved this exact public/synthetic text and paid call')
    parser.set_defaults(func=command)
