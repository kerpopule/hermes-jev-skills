"""Where the TypeSafe key lives, and how to read or store it without showing it.

Resolution order (first hit wins):
  1. ``TYPESAFE_API_KEY`` in the process environment
  2. the OS secret store (macOS Keychain, or ``secret-tool`` on Linux)
  3. ``~/.config/jev/credentials`` (mode 0600), the portable fallback

Nothing in this module prints, logs, or returns the key to a caller that did not
ask for it by name, and ``describe()`` only ever reports presence and length.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

ENV_VAR = "TYPESAFE_API_KEY"
KEYCHAIN_SERVICE = "Hermes TypeSafe API"
KEYCHAIN_ACCOUNT = ENV_VAR
_SECURITY = "/usr/bin/security"

# Jev can also be reached through OpenRouter, which is one key instead of two for anyone
# already using OpenRouter for their models. Same request, same answers: only the URL and
# the model id differ (see client.OPENROUTER_ENDPOINT). Contributed as
# github.com/kerpopule/hermes-jev-skills/pull/1 by Lorenzo DZ (@Barba2k2).
# Venice serves Jev itself, as a first-class decision modality, and currently at zero price
# (/models?type=decision -> jev-latest, priced 0 usd / 0 diem). Same decisions, same shapes.
# Appended LAST so no existing install changes where its decisions are routed: an install that
# already resolves through TypeSafe or OpenRouter keeps doing exactly that.
PROVIDERS = ("typesafe", "openrouter", "venice")
_ENV = {"typesafe": ENV_VAR, "openrouter": "OPENROUTER_API_KEY",
        "venice": "VENICE_API_KEY"}
_SERVICE = {"typesafe": KEYCHAIN_SERVICE, "openrouter": "Hermes OpenRouter API",
            "venice": "Hermes Venice API"}
# Each provider beyond TypeSafe keeps its own 0600 file beside the original.
_CREDENTIAL_FILE = {"openrouter": "credentials-openrouter", "venice": "credentials-venice"}


def credentials_file() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "jev" / "credentials"


def credentials_file_for(provider: str = "typesafe") -> Path:
    """The credentials file for one provider. TypeSafe keeps the original filename."""
    name = _CREDENTIAL_FILE.get(provider)
    return credentials_file().with_name(name) if name else credentials_file()


def looks_like_key(value: str) -> bool:
    """Cheap shape check so an obvious paste mistake is caught before storing."""
    value = value.strip()
    return 20 <= len(value) <= 512 and not any(c.isspace() for c in value) and value.isprintable()


# ── read ─────────────────────────────────────────────────────────────────────

def _from_keychain(provider: str = "typesafe") -> Optional[str]:
    if sys.platform == "darwin" and os.path.exists(_SECURITY):
        cmd = [_SECURITY, "find-generic-password", "-w", "-s", _SERVICE[provider], "-a", _ENV[provider]]
    elif shutil.which("secret-tool"):
        cmd = ["secret-tool", "lookup", "service", _SERVICE[provider], "account", _ENV[provider]]
    else:
        return None
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=8, check=False)
    except Exception:  # noqa: BLE001 - a broken secret store must never crash a caller
        return None
    value = proc.stdout.strip()
    return value if proc.returncode == 0 and value else None


def _from_file(provider: str = "typesafe") -> Optional[str]:
    path = credentials_file_for(provider)
    variable = _ENV.get(provider, ENV_VAR)
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith(variable + "="):
                return line.split("=", 1)[1].strip() or None
    except OSError:
        return None
    return None


def _for(provider: str) -> Optional[str]:
    if provider == "typesafe":
        return (os.environ.get(ENV_VAR) or "").strip() or _from_keychain() or _from_file()
    env = (os.environ.get(_ENV[provider]) or "").strip()
    return env or _from_keychain(provider) or _from_file(provider)


def provider() -> str:
    """Which provider this machine can actually reach Jev through.

    TypeSafe first, always: an existing install must not start routing its decisions
    somewhere else because an OpenRouter key happens to be in the environment for a text
    model. OpenRouter is the fallback, not a preference.
    """
    for name in PROVIDERS:
        if _for(name):
            return name
    return "absent"


def resolve(for_provider: Optional[str] = None) -> Optional[str]:
    """The key. With no argument, the key for whichever provider this machine has."""
    if for_provider:
        if for_provider not in PROVIDERS:
            return None
        return _for(for_provider)
    name = provider()
    return _for(name) if name != "absent" else None


def source(for_provider: Optional[str] = None) -> str:
    name = for_provider or provider()
    if name not in PROVIDERS:
        return "absent"
    if (os.environ.get(_ENV[name]) or "").strip():
        return "environment"
    if _from_keychain(name):
        return "os-secret-store"
    if _from_file(name):
        return "credentials-file"
    return "absent"


def describe() -> Dict[str, object]:
    name = provider()
    key = resolve()
    return {"present": bool(key), "provider": name, "source": source(),
            "length": len(key) if key else 0}


# ── write ────────────────────────────────────────────────────────────────────

def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write beside the target and rename, so a crash can never leave a half-written .env.
    temp = path.with_name(path.name + ".jev-tmp")
    fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(temp, stat.S_IRUSR | stat.S_IWUSR)
    os.replace(temp, path)


def upsert_env_file(path: Path, value: str, variable: str = ENV_VAR) -> None:
    """Set one variable in a dotenv file, leaving every other line untouched."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    entry = f"{variable}={value}"
    replaced = False
    for index, line in enumerate(lines):
        if line.startswith(variable + "="):
            lines[index] = entry
            replaced = True
    if not replaced:
        lines.append(entry)
    _write_private(path, "\n".join(lines) + "\n")


def _store_keychain(value: str, provider: str = "typesafe") -> bool:
    if sys.platform == "darwin" and os.path.exists(_SECURITY):
        # `security` has no stdin mode for the secret, so it is briefly an argv entry
        # of a child we own. The alternative (no secret store at all) is worse.
        cmd = [_SECURITY, "add-generic-password", "-U", "-s", _SERVICE[provider], "-a", _ENV[provider], "-w", value]
        stdin = None
    elif shutil.which("secret-tool"):
        cmd = ["secret-tool", "store", "--label", _SERVICE[provider], "service", _SERVICE[provider],
               "account", _ENV[provider]]
        stdin = value
    else:
        return False
    try:
        proc = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=15, check=False)
    except Exception:  # noqa: BLE001
        return False
    return proc.returncode == 0


def hermes_env_files(hermes_home: Optional[Path] = None) -> List[Path]:
    """Every dotenv a Hermes lane reads. A lane resolves ${VAR} from its OWN .env."""
    home = hermes_home or Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    if not home.is_dir():
        return []
    files = [home / ".env"]
    profiles = home / "profiles"
    if profiles.is_dir():
        files += sorted(p / ".env" for p in profiles.iterdir() if p.is_dir() and not p.name.startswith("."))
    return files


def store(value: str, hermes: bool = True, hermes_home: Optional[Path] = None,
          provider: str = "typesafe") -> Dict[str, object]:
    """Persist the key. Returns where it went, never the key itself."""
    value = value.strip()
    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider: choose one of {', '.join(PROVIDERS)}")
    if not looks_like_key(value):
        raise ValueError("that does not look like an API key")
    written: List[str] = []
    if _store_keychain(value, provider):
        written.append("os-secret-store")
    elif provider == "typesafe":
        upsert_env_file(credentials_file(), value)
        written.append(str(credentials_file()))
    else:
        # A 0600 file next to the TypeSafe one, under this provider's own variable name.
        path = credentials_file_for(provider)
        upsert_env_file(path, value, _ENV[provider])
        written.append(str(path))
    lanes = 0
    if hermes:
        for env_file in hermes_env_files(hermes_home):
            upsert_env_file(env_file, value, _ENV[provider])
            lanes += 1
    return {"stored_in": written, "hermes_env_files": lanes, "provider": provider, "length": len(value)}
