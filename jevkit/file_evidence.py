"""Bounded local evidence, with optional public-data-only policy advice.

No crawling, shell execution, implicit network or action permission. The caller supplies
an explicit relative-path manifest (for example from its existing local search). Paths
and content stay local by default. An absent shortlist never proves absent evidence.
"""
from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Sequence

from . import decide, policy, privacy, rerank

MAX_FILES = 128
MAX_BYTES = 65536
MAX_TOTAL_BYTES = 1048576
MAX_QUERY = 500
TEXT_SUFFIXES = {'.py', '.js', '.ts', '.tsx', '.jsx', '.md', '.rst', '.txt', '.json',
                 '.toml', '.yaml', '.yml', '.sh', '.css', '.html', '.go', '.rs', '.java'}
EXCLUDED = {'.git', '.hg', '.svn', 'node_modules', '__pycache__', '.venv', 'venv',
            'dist', 'build', 'vendor', 'private', 'customer', 'customers', 'secrets'}
PRIVATE = re.compile(r'(?i)\b(private|confidential|customer|patient|student|credential)s?\b')
WORDS = re.compile(r'[a-zA-Z][a-zA-Z0-9_]{1,63}')


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _relative(raw: str) -> PurePosixPath:
    if not isinstance(raw, str) or len(raw) > 512 or '\\' in raw or '\x00' in raw:
        raise ValueError('invalid_path')
    path = PurePosixPath(raw)
    if path.is_absolute() or not path.parts or any(p in {'.', '..'} for p in raw.split('/')):
        raise ValueError('invalid_path')
    if any(p.startswith('.') or p.lower() in EXCLUDED or PRIVATE.search(p) for p in path.parts):
        raise ValueError('excluded')
    if privacy.is_sensitive(raw) or path.suffix.lower() not in TEXT_SUFFIXES:
        raise ValueError('excluded')
    return path


def _open_root(root: str) -> int:
    # Walk every ancestor without following links, not just the final component.
    path = Path(os.path.abspath(root))
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read(fd: int, raw: str) -> str:
    path = _relative(raw)
    current = os.dup(fd)
    try:
        for part in path.parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            os.close(current)
            current = child
        file_fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=current)
        try:
            before = os.fstat(file_fd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise ValueError('not_regular_or_hardlinked')
            if before.st_size > MAX_BYTES:
                raise ValueError('oversized')
            with os.fdopen(file_fd, 'rb', closefd=False) as stream:
                raw_bytes = stream.read(MAX_BYTES + 1)
            after = os.fstat(file_fd)
            if len(raw_bytes) > MAX_BYTES or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError('changed_or_oversized')
            if b'\x00' in raw_bytes:
                raise ValueError('binary')
            text = raw_bytes.decode('utf-8')
            if any(ord(c) < 32 and c not in '\n\r\t' for c in text):
                raise ValueError('binary')
            return text
        finally:
            os.close(file_fd)
    finally:
        os.close(current)


def _screen(text: str) -> str:
    # Screen the entire bounded file BEFORE choosing any snippets or making requests.
    if privacy.is_sensitive(text) or PRIVATE.search(privacy.normalize(text)):
        return 'sensitive'
    if privacy.redact(text, len(text) + 1) != privacy.normalize(text):
        return 'personal_or_secret_shape'
    if rerank.local_screen(text, unvetted=True):
        return 'untrusted_instructions'
    return ''


def _terms(text: str) -> set:
    return {w.lower() for w in WORDS.findall(text)}


def scout(root: str, paths: Sequence[str], query: str, *, top_k: int = 8) -> Dict[str, Any]:
    """Lexical pre-flight over an explicit manifest; no model call, ever."""
    if not isinstance(query, str) or not query or len(query) > MAX_QUERY:
        raise ValueError('query must contain 1..500 characters')
    if not isinstance(paths, (list, tuple)) or len(paths) > MAX_FILES:
        raise ValueError('manifest must be a list of at most 128 relative paths; batch larger searches')
    if not isinstance(top_k, int) or isinstance(top_k, bool) or not 1 <= top_k <= 32:
        raise ValueError('top_k must be 1..32')
    terms = _terms(query)
    rows, unknown, total = [], [], 0
    fd = _open_root(root)
    try:
        for raw in dict.fromkeys(paths):
            try:
                if total + MAX_BYTES > MAX_TOTAL_BYTES:
                    raise ValueError('read_budget')
                text = _read(fd, raw)
                total += len(text.encode())
                blocked = _screen(text)
                if blocked:
                    raise ValueError(blocked)
                lines = text.splitlines()
                matches = [(i, len(terms & _terms(line))) for i, line in enumerate(lines, 1)]
                hits = sorted(((i, score) for i, score in matches if score), key=lambda x: (-x[1], x[0]))
                start = hits[0][0] if hits else 1
                digest = _hash(text)
                evidence_id = 'E' + _hash(raw + '\x00' + digest)[:24]
                rows.append({'id': evidence_id, 'path': raw, 'sha256': digest,
                             'start_line': start, 'end_line': min(len(lines), start + 7),
                             'relevance': max((score for _, score in hits), default=0),
                             'assessment': 'lexical_match' if hits else 'unknown'})
            except (OSError, UnicodeError, ValueError) as error:
                reason = str(error) if isinstance(error, ValueError) and not isinstance(error, UnicodeError) else 'unreadable'
                unknown.append({'path': raw, 'reason': reason})
    finally:
        os.close(fd)
    rows.sort(key=lambda r: (-r['relevance'], r['path']))
    selected = [r['id'] for r in rows if r['relevance']][:top_k]
    return {'source': 'code', 'sent_to_jev': False, 'selected_ids': selected, 'evidence': rows,
            'unknown': unknown, 'not_shortlisted_ids': [r['id'] for r in rows if r['id'] not in selected],
            'coverage': 'explicit_manifest_only', 'absence_proven': False,
            'note': 'Not shortlisted means unjudged, not irrelevant. Expand local search and read references.',
            'bytes_read': total}


def recover(root: str, reference: Dict[str, Any]) -> Dict[str, Any]:
    """Targeted local read, pinned to content hash and local reference ID; no network."""
    fd = _open_root(root)
    try:
        text = _read(fd, reference['path'])
    finally:
        os.close(fd)
    if _screen(text):
        raise ValueError('reference is no longer eligible')
    digest = _hash(text)
    expected = 'E' + _hash(reference['path'] + '\x00' + digest)[:24]
    if digest != reference['sha256'] or expected != reference['id']:
        raise ValueError('stale_reference')
    start, end = reference['start_line'], reference['end_line']
    if any(isinstance(v, bool) or not isinstance(v, int) for v in (start, end)) or not 1 <= start <= end <= start + 7:
        raise ValueError('invalid_line_range')
    return {'id': expected, 'start_line': start, 'end_line': end,
            'text': '\n'.join(text.splitlines()[start - 1:end]), 'untrusted': True}


def public_advice(text: str, *, purpose: str, query: str = '', approved_public: bool = False,
                  transport=None) -> Dict[str, Any]:
    """One optional bounded policy reading, not a gate. Consent is caller-owned.

    Invoke ONLY on exact operator-reviewed public/synthetic text with spend approval.
    The flag is not a privacy detector. Unlabelled source/logs must remain local.
    """
    if purpose not in {'file-relevance', 'failure-symptom'}:
        raise ValueError('invalid purpose')
    result = {'action': 'unknown', 'source': 'code', 'sent_to_jev': False}
    if approved_public is not True:
        return {**result, 'reason': 'public_data_and_spend_approval_required'}
    if len(text) > 3000 or len(query) > MAX_QUERY or _screen(text + '\n' + query):
        return {**result, 'reason': 'withheld_locally'}
    # Paths/URLs are not needed for this judgement; withhold rather than guessing at redaction.
    if re.search(r'(?:https?://|[/\\]|\b[A-Za-z]:)', text + query):
        return {**result, 'reason': 'path_or_url_withheld'}
    # Pin shipped schema: a local policy override must not introduce arbitrary outbound fields.
    rules = policy.load(str(policy.SHIPPED / (purpose + '.json')))
    decision = decide.decide({'excerpt': text, 'query': query, 'id': 'D0'}, rules,
                             timeout=4.0, retries=0, transport=transport, record=False)
    if decision.get('source') == 'jev' and decision.get('drift') is not False:
        decision.update(action='unknown', source='fallback', fallback_used=True,
                        error='unknown_or_changed_model_version')
    return {**decision, 'advisory_only': True, 'proven_root_cause': False}
