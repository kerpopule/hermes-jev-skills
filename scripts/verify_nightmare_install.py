#!/usr/bin/env python3
"""Read-only verification of root-installed candidate, with no keys or model calls."""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOME = Path.home() / ".hermes"
pairs = []
for plugin in ("hermes-jev", "hermes-handoff"):
    for name in ("client.py", "decision_trace.py", "search.py", "gate.py", "cli.py"):
        pairs.append((ROOT / "jevkit" / name, HOME / "plugins" / plugin / "jevkit" / name))
for skill in ("jev-search", "jev-browser-use", "jev-model-routing"):
    pairs.append((ROOT / "skills" / skill / "SKILL.md", HOME / "skills/jev" / skill / "SKILL.md"))
pairs.append((ROOT / "skills/jev-browser-use/scripts/jev_browser_agent.py",
              HOME / "skills/jev/jev-browser-use/scripts/jev_browser_agent.py"))
for source, installed in pairs:
    assert source.read_bytes() == installed.read_bytes(), "installed byte mismatch: " + installed.name
sys.path.insert(0, str(HOME / "plugins/hermes-jev"))
from jevkit import client, decision_trace, gate
assert Path(client.__file__).resolve().is_relative_to(HOME / "plugins/hermes-jev")
fixture = [{"id": "installed_fixture", "questions": {"q": client.noul("Synthetic fixture")},
            "response": '{"answers":{"q":{"type":"noul","noul":1}}}'}]
assert decision_trace.replay(fixture, decision_trace.record(fixture))["matches"] == 1
try:
    client.validate_reply(b'{"answers":{"q":{"type":"noul","noul":0,"noul":1}}}', fixture[0]["questions"])
except client.JevError as error:
    assert error.code == "malformed"
else:
    raise AssertionError("installed decoder accepted duplicate members")
assert gate.tiebreak_verdict({"action": "approve", "policy": "gate-strict@1", "fallback_used": True}) == "escalate"
request = {"question": "Public release?", "sources": [{"id": "docs", "description": "Public docs"}],
           "plans": [{"id": "one", "query": "release", "sources": ["docs"], "time_range": "month"}]}
run = subprocess.run([str(HOME / "bin/jev"), "search-plan"], input=json.dumps(request),
                     capture_output=True, text=True, timeout=15)
assert run.returncode == 0
result = json.loads(run.stdout)
assert result["plan"] == request["plans"][0] and not result["jev_called"]
print(json.dumps({"passed": True, "matched_files": len(pairs), "fresh_installed_import": True,
                  "fresh_cli": True, "duplicate_member_refused": True, "offline_replay": True,
                  "provider_calls": 0, "gateway_loaded": "not_asserted",
                  "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()}, indent=2))
