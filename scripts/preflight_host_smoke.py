#!/usr/bin/env python3
"""Fresh-host pre-call smoke. Held work is synthetic; --live uses public text.

No gateway reload, other-profile writes, owner messages or credential output.
An explicit --live call uses the existing configured Jev account; no new service.
"""
import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest import mock


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hermes-source', type=Path, required=True)
    parser.add_argument('--plugin-source', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()
    if args.out.exists():
        raise ValueError('refuse existing receipt')
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix='jev-preflight-host-') as folder:
        home = Path(folder)
        os.environ['HERMES_HOME'] = str(home)
        source = args.plugin_source.resolve()
        target = home / 'plugins/hermes-jev'
        shutil.copytree(source, target, ignore=shutil.ignore_patterns('__pycache__'))
        library = source / 'jevkit'
        if not library.is_dir():
            shutil.copytree(root / 'jevkit', target / 'jevkit')
        (home / 'skills').mkdir()
        # Published repository procedures only, never private owner catalogs.
        (home / 'skills/jev').symlink_to(root / 'skills', target_is_directory=True)
        (home / 'config.yaml').write_text('plugins:\n  enabled: [hermes-jev]\n')
        (home / 'jev').mkdir()
        (home / 'jev/state.json').write_text(json.dumps({
            'skills': 'on', 'routing': 'off', 'skill_feedback': 'off'}))
        sys.path.insert(0, str(args.hermes_source.resolve()))
        from hermes_cli.plugins import PluginManager
        manager = PluginManager()
        manager.discover_and_load()
        p = manager._plugins['hermes-jev'].module
        assert p is not None
        assert manager.has_hook('pre_llm_call')
        release, finished = threading.Event(), threading.Event()
        def held(*unused_args, **unused_kwargs):
            release.wait(5)
            finished.set()
            return {'status': 'ok', 'skills': []}
        with mock.patch.object(p.skillpick, 'pick', side_effect=held):
            start = time.monotonic()
            try:
                held_result = manager.invoke_hook('pre_llm_call', session_id='synthetic-held',
                    turn_id='1', user_message='Synthetic public scheduler task')
                held_seconds = time.monotonic() - start
                assert held_seconds < 2, held_seconds
                assert not finished.is_set(), 'not held through native return'
                assert not any(held_result), held_result
                p._TURNS['synthetic-held'] = {'turn_id': 'new', 'route_answers': None}
            finally:
                release.set()
            assert finished.wait(2)
        assert p._TURNS['synthetic-held'] == {'turn_id': 'new', 'route_answers': None}
        live = {'performed': False}
        if args.live:
            start = time.monotonic()
            value = manager.invoke_hook('pre_llm_call', session_id='synthetic-public', turn_id='1',
                user_message='Rank search results for relevance and identify unsafe instructions in public web snippets.')
            elapsed = time.monotonic() - start
            records = [json.loads(line) for line in (home / 'logs/jev-decisions.jsonl').read_text().splitlines()]
            # Success must be an actual selection result, not a timeout or no-key fallback.
            success = [r for r in records if r.get('kind') == 'skill' and r.get('status') == 'ok']
            assert success, [{k: r.get(k) for k in ('kind', 'status', 'reason_code')} for r in records]
            assert elapsed < 2, elapsed
            live = {'performed': True, 'seconds': round(elapsed, 4),
                    'selection_status': 'ok', 'suggestion_returned': any(bool(v) for v in value)}
        result = {'schema': 'jev.preflight_host_v1', 'real_plugin_manager': True,
                  'native_hook_dispatch': True, 'held_seconds': round(held_seconds, 4),
                  'held_work_outlived_return': True, 'late_turn_state_unchanged': True,
                  'plugin_sha256': hashlib.sha256((source / '__init__.py').read_bytes()).hexdigest(),
                  'preflight_sha256': hashlib.sha256((target / 'jevkit/preflight.py').read_bytes()).hexdigest(),
                  'live_public_text_inference': live, 'gateway_reload_claimed': False}
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
