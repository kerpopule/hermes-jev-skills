#!/usr/bin/env python3
"""Verify installed bytes and fresh offline entrypoints."""
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOME = Path.home() / '.hermes'
pairs = []
for plugin in ('hermes-jev', 'hermes-handoff'):
    for name in ('search.py', 'gate.py', 'cli.py'):
        pairs.append((ROOT / 'jevkit' / name, HOME / 'plugins' / plugin / 'jevkit' / name))
for skill in ('jev-search', 'jev-browser-use', 'jev-model-routing'):
    pairs.append((ROOT / 'skills' / skill / 'SKILL.md', HOME / 'skills/jev' / skill / 'SKILL.md'))
pairs.append((ROOT / 'skills/jev-browser-use/scripts/jev_browser_agent.py', HOME / 'skills/jev/jev-browser-use/scripts/jev_browser_agent.py'))
for source, installed in pairs:
    assert source.read_bytes() == installed.read_bytes(), installed.name
sys.path.insert(0, str(HOME / 'plugins/hermes-jev'))
from jevkit import gate, search
assert Path(search.__file__).resolve().is_relative_to(HOME / 'plugins/hermes-jev')
assert gate.tiebreak_verdict({'action':'approve','policy':'gate-strict@1','fallback_used':True}) == 'escalate'
request = {'question':'Public release?', 'sources':[{'id':'docs','description':'Public docs'}], 'plans':[{'id':'one','query':'release','sources':['docs'],'time_range':'month'}]}
command = [str(HOME / 'bin/jev'), 'search-plan']
run = subprocess.run(command, input=json.dumps(request), capture_output=True, text=True, timeout=15)
assert run.returncode == 0, run.stderr
result = json.loads(run.stdout)
assert result['plan'] == request['plans'][0] and not result['jev_called']
receipt = {'passed':True,'matched_files':len(pairs),'fresh_installed_import':True,'fresh_cli':True,'provider_calls':0,'gateway_loaded':'not_asserted','source_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()}
(ROOT / 'evidence/ecosystem/install.json').write_text(json.dumps(receipt,indent=2)+'\n')
print(json.dumps(receipt))
