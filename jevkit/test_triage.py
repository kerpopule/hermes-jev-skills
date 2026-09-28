"""Observed test symptoms, never a root-cause verdict or a replacement for tests."""
from __future__ import annotations

import re
from typing import Any, Dict

from . import file_evidence as evidence

PATTERNS = {
    'assertion': r'\b(?:AssertionError|assertion failed|expected .{1,80} but got)\b',
    'dependency': r'\b(?:ModuleNotFoundError|ImportError|cannot find module)\b',
    'syntax': r'\b(?:SyntaxError|IndentationError|unexpected token)\b',
    'timeout': r'\b(?:TimeoutError|timed out|deadline exceeded)\b',
    'permission': r'\b(?:PermissionError|permission denied|EACCES)\b',
    'network': r'\b(?:ConnectionRefusedError|connection refused|ECONNRESET|ENOTFOUND)\b',
    'resource': r'\b(?:MemoryError|out of memory|no space left on device|ENOSPC)\b',
}


def classify(state: Dict[str, Any], *, transport=None) -> Dict[str, Any]:
    if not isinstance(state, dict):
        raise ValueError('test failure must be an object')
    text, exit_code = state.get('error'), state.get('exit_code')
    if not isinstance(text, str) or len(text.encode()) > evidence.MAX_BYTES:
        raise ValueError('error must be UTF-8 text of at most 65536 bytes')
    if isinstance(exit_code, bool) or not isinstance(exit_code, int) or not -255 <= exit_code <= 255:
        raise ValueError('exit_code must be an integer from -255 to 255')
    digest = evidence._hash(text)
    out = {'category': 'unknown', 'source': 'code', 'sent_to_jev': False,
           'exit_code': exit_code, 'test_failed': exit_code != 0, 'proven_root_cause': False,
           'evidence': {'id': 'T' + digest[:24], 'sha256': digest, 'start_line': 1,
                        'end_line': max(1, len(text.splitlines()))},
           'next_step': 'Inspect the original bounded log and relevant source; rerun the actual failing test.',
           'advisory_only': True}
    blocked = evidence._screen(text)
    if blocked:
        return {**out, 'reason': blocked}
    if exit_code == 0:
        return {**out, 'reason': 'successful_exit_not_failure_evidence'}
    # A log can quote documentation or say an error did NOT happen. Do not turn
    # those mentions into a positive symptom (conservative abstention, not NLP proof).
    if re.search(r'\b(?:documentation|example|hypothetical|caught|handled)\b|'
                 r'\b(?:no|not|without)\s+\w*(?:Error|failure|timeout)\b|'
                 r'\b\w*Error\s+(?:(?:was|is|were)\s+not|did\s+not|never)\b|'
                 r'\bexpected\s+(?:exception|error)\b|'
                 r'\b(?:previous|prior|last|earlier)\s+(?:run|execution)\b|'
                 r'\b(?:string|literal|test\s+input)\b.{0,80}\b\w*Error\b|'
                 r'\b\w*Error\b.{0,80}\b(?:string|literal|test\s+input)\b', text, re.I):
        return {**out, 'reason': 'non_observation_or_negation'}
    matched = {name: [i for i, line in enumerate(text.splitlines(), 1)
                      if re.search(pattern, line, re.I)] for name, pattern in PATTERNS.items()}
    matched = {name: lines for name, lines in matched.items() if lines}
    out['observed_symptoms'] = matched
    if len(matched) == 1:
        out['category'] = next(iter(matched))
        out['evidence']['start_line'] = matched[out['category']][0]
        out['evidence']['end_line'] = out['evidence']['start_line']
        return out
    if matched:
        return {**out, 'reason': 'competing_symptoms_require_investigation'}
    # Do not pay for what the deterministic taxonomy already says. Unknowns may be
    # considered only with explicit approval for this exact public/synthetic excerpt.
    if state.get('semantic') is True:
        advice = evidence.public_advice(text, purpose='failure-symptom',
                                       approved_public=state.get('approved_public') is True,
                                       transport=transport)
        out['advice'] = advice
        out['sent_to_jev'] = advice['sent_to_jev']
        if advice.get('source') == 'jev' and advice['action'] in PATTERNS:
            out.update(category=advice['action'], source='jev')
    return out
