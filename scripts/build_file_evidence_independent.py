#!/usr/bin/env python3
"""Build frozen PUBLIC CPython evidence and synthetic failure validation; no Jev calls.

Network is restricted to version-pinned public CPython source. Executed fixtures use
only bounded local Python operations; OS/resource symptoms are textual fixtures.
"""
import ast
import hashlib
import json
import re
from pathlib import Path
import subprocess
import sys
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'evals/file-evidence/independent-v2'
VERSION = 'v3.11.9'
SELECTION = {
    'functools': ['update_wrapper', 'wraps', 'lru_cache', 'cached_property.__get__'],
    'queue': ['Queue.put', 'Queue.get', 'Queue.task_done', 'Queue.join'],
    'concurrent/futures/_base': ['Future.cancel', 'Future.result', 'Future.set_result', 'Future.add_done_callback'],
    'gzip': ['GzipFile.write', 'GzipFile.read', '_GzipReader._read_eof', 'GzipFile.close'],
    'tempfile': ['_mkstemp_inner', 'mkstemp', 'TemporaryDirectory.cleanup', 'TemporaryDirectory.__exit__'],
    'shutil': ['copyfile', 'copytree', 'rmtree', 'move'],
    'pathlib': ['Path.rename', 'Path.replace', 'Path.unlink', 'Path.mkdir'],
    'contextlib': ['ExitStack.__exit__', '_BaseExitStack.enter_context', '_BaseExitStack.callback', 'ExitStack.pop_all'],
}
# pop_all belongs to the base class on CPython 3.11.
SELECTION['contextlib'][-1] = '_BaseExitStack.pop_all'
QUERIES = [
    ('wrapper', 'How are wrapped function metadata and the original callable preserved?', ['functools:update_wrapper', 'functools:wraps']),
    ('queue', 'How does queue task completion wake a caller waiting for all queued work?', ['queue:Queue.task_done', 'queue:Queue.join']),
    ('future', 'How does a future return its result to a waiting caller when a producer completes it?', ['concurrent/futures/_base:Future.result', 'concurrent/futures/_base:Future.set_result']),
    ('gzip', 'Where is the gzip checksum and uncompressed length written and verified?', ['gzip:GzipFile.close', 'gzip:_GzipReader._read_eof']),
    ('tempfile', 'How is a temporary directory removed both explicitly and on leaving a context manager?', ['tempfile:TemporaryDirectory.cleanup', 'tempfile:TemporaryDirectory.__exit__']),
    ('copy', 'How does recursive directory copying delegate to single file copying?', ['shutil:copytree', 'shutil:copyfile']),
    ('rename', 'Which path methods rename a file with or without replacing an existing destination?', ['pathlib:Path.rename', 'pathlib:Path.replace']),
    ('exitstack', 'How does ExitStack acquire a context and unwind exit callbacks in reverse order?', ['contextlib:_BaseExitStack.enter_context', 'contextlib:ExitStack.__exit__']),
]


def sha(blob):
    return hashlib.sha256(blob).hexdigest()


def named_functions(tree, prefix=''):
    found = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found[prefix + node.name] = node
        elif isinstance(node, ast.ClassDef):
            found.update(named_functions(node, prefix + node.name + '.'))
    return found


def build():
    if OUT.exists():
        raise SystemExit('refusing to overwrite an existing validation corpus')
    sources, files = [], []
    for module, names in SELECTION.items():
        url = 'https://raw.githubusercontent.com/python/cpython/%s/Lib/%s.py' % (VERSION, module)
        with urllib.request.urlopen(url, timeout=30) as response:
            raw = response.read(1000000)
        text = raw.decode('utf-8')
        nodes = named_functions(ast.parse(text))
        sources.append({'module': module, 'url': url, 'sha256': sha(raw), 'license': 'PSF-2.0'})
        for name in names:
            node = nodes[name]
            excerpt = '\n'.join(text.splitlines()[node.lineno - 1:node.end_lineno]) + '\n'
            files.append({'id': module + ':' + name,
                          'path': module.replace('/', '_') + '_' + name.replace('.', '_') + '.py',
                          'source_start_line': node.lineno, 'source_end_line': node.end_lineno,
                          'text': excerpt, 'sha256': sha(excerpt.encode()), 'module': module})
    failures = []
    operations = [
        ('assertion', "assert 2 + 2 == 5, 'arithmetic invariant'"),
        ('assertion', "import unittest; unittest.TestCase().assertEqual({'ready': True}, {'ready': False})"),
        ('assertion', "import unittest; unittest.TestCase().assertIn('green', ['red', 'blue'])"),
        ('assertion', "import unittest; unittest.TestCase().assertAlmostEqual(0.1, 0.3)"),
        ('dependency', 'import jev_fixture_absent_module_493'),
        ('dependency', 'from collections import jev_fixture_absent_symbol'),
        ('dependency', "import importlib; importlib.import_module('jev_fixture_absent_package_842.child')"),
        ('dependency', 'from json import jev_fixture_absent_decoder'),
        ('syntax', "compile('def broken(\\n', '<fixture>', 'exec')"),
        ('syntax', "compile('if True\\n    pass', '<fixture>', 'exec')"),
        ('syntax', "compile('    x = 1', '<fixture>', 'exec')"),
        ('syntax', "compile('return 1', '<fixture>', 'exec')"),
        ('timeout', 'from concurrent.futures import Future; Future().result(timeout=0)'),
        ('timeout', 'from concurrent.futures import Future; Future().exception(timeout=0)'),
        ('unknown', 'int("not-a-number")'),
        ('unknown', '{}["missing"]'),
        ('unknown', 'next(iter([]))'),
        ('unknown', '1 + None'),
        ('unknown', '1 / 0'),
        ('unknown', 'import json; json.loads("{")'),
        ('unknown', 'import struct; struct.unpack("i", b"")'),
        ('unknown', 'import re; re.compile("[")'),
        ('unknown', 'import queue; queue.Queue().get_nowait()'),
        ('unknown', 'import math; math.sqrt(-1)'),
    ]
    for i, (label, code) in enumerate(operations):
        # Isolated interpreter, no user packages/site startup, no filesystem writes or sockets.
        p = subprocess.run([sys.executable, '-I', '-S', '-c', code], capture_output=True, text=True, timeout=5)
        if p.returncode == 0 or not p.stderr:
            raise RuntimeError('fixture did not actually fail')
        # Publish basenames, never host installation paths. Keep every diagnostic line.
        diagnostic = re.sub(r'File "([^"]+)"',
                            lambda m: 'File "' + Path(m.group(1)).name + '"', p.stderr)
        failures.append({'id': 'executed-%02d' % i, 'label': label, 'text': diagnostic,
                         'exit_code': p.returncode, 'fixture_code': code,
                         'provenance': 'actual isolated CPython stderr; traceback file paths normalized to basename'})
    authored = {
        'assertion': [
            'FAIL test_cart_total\nExpected: 19\nReceived: 21\nTests: 1 failed, 8 passed',
            'org.opentest4j.AssertionFailedError: expected: <ready> but was: <pending>\nTests run: 12, Failures: 1',
            "thread 'sum_is_stable' panicked at fixture.rs:8\nassertion `left == right` failed\n  left: 14\n right: 12",
            'FAIL: test_result\nArrays are not equal\nMismatched elements: 2 of 3\nACTUAL: [1, 5, 9]\nDESIRED: [1, 4, 8]',
        ],
        'dependency': [
            "Error: Cannot find package 'fixture-lib' imported from test.mjs\ncode: ERR_MODULE_NOT_FOUND",
            'error[E0432]: unresolved import `fixture_crate`\nuse of unresolved module or unlinked crate',
            'java.lang.NoClassDefFoundError: fixture.Widget\nCaused by: java.lang.ClassNotFoundException: fixture.Widget',
            'error TS2307: Cannot find module fixture-lib or its corresponding type declarations.',
        ],
        'syntax': [
            'error: expected `;`, found `}`\ncompilation aborted due to previous error',
            'test.mjs:4\nconst value = ;\n              ^\nSyntaxError: Unexpected token ;',
            'compiler: error: illegal start of expression\nCompilation failure',
            'error TS1005: closing parenthesis expected.\nFound 1 error.',
        ],
        'timeout': [
            'Test timed out in 30000ms.\nWaiting for locator to become visible.\n1 failed',
            'FAIL test_worker\nExceeded timeout of 5000 ms for a test.\n1 test suite failed',
            'org.junit.runners.model.TestTimedOutException: test timed out after 1000 milliseconds',
            'context deadline exceeded\noperation did not finish before the test deadline',
        ],
        'permission': [
            'EACCES: permission denied, open fixture.lock\nprocess exited with code 1',
            'java.nio.file.AccessDeniedException: fixture.dat\nTests run: 1, Errors: 1',
            'Operation not permitted (os error 1)\nUnable to replace read-only fixture',
            'System.UnauthorizedAccessException: Access to fixture.dat is denied.',
        ],
        'network': [
            'Error: connect ECONNREFUSED\nThe fixture listener is not accepting connections.',
            'getaddrinfo EAI_AGAIN fixture.invalid\nName resolution temporarily failed',
            'java.net.SocketException: Connection reset\nTest transport failed',
            'TLS handshake failed: remote peer closed the connection\nIntegration test failed',
        ],
        'resource': [
            'ENOSPC: no space left on device, write fixture.dat\nBuild failed',
            'java.lang.OutOfMemoryError: Java heap space\nTest runner terminated',
            'allocation failed: memory limit exceeded\nworker exit status 137',
            'Disk quota exceeded (os error 122)\nFixture output could not be written',
        ],
        'unknown': [
            'AssertionError was expected and caught.\nThe runner later stopped with no diagnostic.',
            'Previous run: TimeoutError.\nCurrent run: exit status 1, no stderr captured.',
            'Documentation example: PermissionError\nActual result unavailable.',
            'Dependency loaded successfully. No network error occurred.\nRunner reported failure without details.',
            'AssertionError: values differ\nMemoryError: allocator exhausted\nTwo failures from separate workers',
            'SyntaxError: invalid token\nConnectionRefusedError: listener unavailable\nTwo independent test processes failed',
            'Test command exited 2 with empty diagnostics.',
            'SIGSEGV received. Core dump disabled. No exception message.',
            'Expected exception: TimeoutError\nTest passed',
            'All 120 tests passed.\n0 failures',
            'String under test is AssertionError.\nHarness result missing.',
            'Worker exited 137. No resource usage or error diagnostics were captured.',
        ],
    }
    for label, examples in authored.items():
        for i, text in enumerate(examples):
            failures.append({'id': 'authored-%s-%02d' % (label, i), 'label': label, 'text': text,
                             'exit_code': 0 if label == 'unknown' and i in (8, 9) else 7,
                             'provenance': 'public synthetic, author-created conventional multi-language diagnostic, NOT captured project CI'})
    corpus = {'version': 2, 'split': 'validation-only; A/B are tuning, no v2 model scoring before freeze',
              'independence_limit': 'Task author chose labels; not blind human annotation or real-project CI sampling.',
              'sources': sources, 'files': files,
              'scouting': [{'id': name, 'query': query, 'relevant': gold, 'critical': gold} for name, query, gold in QUERIES],
              'failures': failures, 'python': sys.version.split()[0]}
    OUT.mkdir(parents=True)
    (OUT / 'corpus.json').write_text(json.dumps(corpus, indent=2) + '\n')
    # PSF license applies to the extracted public source; keep attribution with the corpus.
    with urllib.request.urlopen('https://raw.githubusercontent.com/python/cpython/%s/LICENSE' % VERSION, timeout=30) as r:
        license_text = r.read(100000).decode()
    (OUT / 'CPYTHON-LICENSE.txt').write_text(license_text)
    print(json.dumps({'files': len(files), 'scouting_tasks': len(QUERIES), 'failures': len(failures),
                      'executed_failures': len(operations), 'corpus_sha256': sha((OUT / 'corpus.json').read_bytes())}))


if __name__ == '__main__':
    build()
