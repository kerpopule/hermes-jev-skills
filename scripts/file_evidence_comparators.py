"""Frozen benchmark comparators, not production classifiers or scoring oracles.

No corpus imports or gold-label access. Defaults fixed before validation scoring.
"""
from collections import Counter
import math
import re

STOP = frozenset('a an and are as at be by does for from how in is it of on or the to when where which with without'.split())


def tokens(text):
    # Split underscores and camelCase, unlike the deliberately minimal shipping scout.
    text = re.sub(r'([a-z])([A-Z])', r'\1 \2', text).replace('_', ' ')
    return [word for word in re.findall(r'[a-z][a-z0-9]*', text.lower()) if word not in STOP]


def bm25(files, query, top_k=8):
    """BM25 full file plus relative filename, k1=1.2, b=0.75, deterministic ties."""
    docs = [tokens(row['path'] + '\n' + row['text']) for row in files]
    counts = [Counter(doc) for doc in docs]
    avg = sum(map(len, docs)) / max(1, len(docs)) or 1
    q = set(tokens(query))
    df = {word: sum(word in c for c in counts) for word in q}
    scores = []
    for row, doc, count in zip(files, docs, counts):
        value = 0.0
        for word in q:
            freq = count[word]
            inverse = math.log(1 + (len(docs) - df[word] + 0.5) / (df[word] + 0.5))
            value += inverse * freq * 2.2 / (freq + 1.2 * (0.25 + 0.75 * len(doc) / avg))
        scores.append((value, row['id']))
    scores.sort(key=lambda pair: (-pair[0], pair[1]))
    return [key for score, key in scores[:top_k] if score > 0]


# Broad conventional toolchain signatures. Frozen before corpus scoring, not fitted
# to v2 scores. Matching is conservative across the whole log, never a root cause.
EXTRA = {
    'assertion': r'AssertionFailedError|assertion .* failed|Arrays are not equal|Expected:[\s\S]{0,160}Received:',
    'dependency': r'ERR_MODULE_NOT_FOUND|Cannot find package|unresolved import|NoClassDefFoundError|ClassNotFoundException|TS2307',
    'syntax': r'TS1005|illegal start of expression|error: expected .{1,80} found',
    'timeout': r'Exceeded timeout|TestTimedOutException|deadline exceeded',
    'permission': r'AccessDeniedException|UnauthorizedAccessException|Operation not permitted',
    'network': r'ECONNREFUSED|EAI_AGAIN|Connection reset|TLS handshake failed',
    'resource': r'OutOfMemoryError|memory limit exceeded|Disk quota exceeded',
}


def extended_failure(text, baseline):
    """Retain shipping abstention guards; add signatures only for unguarded records.

Return unknown for competing matches, including contradictions with the baseline.
The baseline object is a deterministic classifier output, never the expected label.
"""
    if baseline.get('reason') or not baseline.get('test_failed'):
        return baseline['category']
    labels = {name for name, pattern in EXTRA.items() if re.search(pattern, text, re.I)}
    if baseline['category'] != 'unknown':
        labels.add(baseline['category'])
    return next(iter(labels)) if len(labels) == 1 else 'unknown'
