"""Vision shadow: the local Jev-Omni model answers the same picture question a vision tool just
answered, off the hot path, and one row of ids, hashes and answer classes is logged.

Jev-Omni (akhilaaa3/Jev-Omni, Apache-2.0; independent work, not TypeSafe's Jev) is a picture
*decision* model: it takes an image, a question and a short list of options and returns one
probability per option. It runs on this machine through its own Python environment
(``JEV_OMNI_DIR``, default ``~/Projects/jev-omni-local``), so the picture never leaves the machine.

What it watches: ``vision_analyze`` calls whose question has a decision shape —

* ``passfail``  the question asks for PASS/FAIL outright;
* ``yesno``     a yes/no question ("Is the logo clipped?", "... yes or no");
* ``qa``        a visual QA request ("Strict QA: no clipping, ..."), asked as PASS/FAIL.

Everything else (describe, transcribe, coordinates, scores) is logged as ``open`` and never sent
to the model. The current model's answer is read from the tool result only to classify it
(PASS / FAIL / Yes / No / unclear); a native-vision result, where the main model looks at the
picture itself, has no answer to read and logs ``current_path: native``.

A row holds ids, the image's sha256, a hash of the question, classes, a confidence, whether the
hybrid rule would have kept the check local (confidence >= ``THRESHOLD``, the cut-off measured on
101 hand-labelled Hermes checks) and timings. Never the picture, the question, the path or the
answer text. Nothing here can change a tool result: the plugin only calls ``snapshot`` (a few
microseconds, one ``stat``) inside the hook and runs ``job`` on the shadow thread.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from . import ledger

try:  # POSIX only; without it the machine-wide lock is skipped, the memory guard still runs
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

FEATURE = "vision"
VISION_TOOLS = ("vision_analyze",)
THRESHOLD = 0.6           # hybrid cut-off; 5-fold cross-validated on the 101-item A/B (92/101, 92% local)
TIMEOUT_S = 120.0         # model load (~3 s warm, longer cold) + one picture (~1 s)
LOCK_WAIT_S = 60.0        # one model in memory at a time across every gateway on the machine
MIN_FREE_GB = 16.0        # the 8-bit model peaks at ~13 GB; skip rather than squeeze the machine
MAX_IMAGE_BYTES = 40 * 1024 * 1024
MAX_QUESTION_CHARS = 2000
MODEL_NAME = "jev-omni-mlx-8bit"
STATE = "The attached image is the item being checked."
OPTIONS = {"passfail": ("PASS", "FAIL"), "qa": ("PASS", "FAIL"), "yesno": ("Yes", "No")}
RUNNER = Path(__file__).resolve().with_name("omni_runner.py")

# ── question shape ───────────────────────────────────────────────────────────

_OPEN = re.compile(
    r"\b(transcribe|summari[sz]e|coordinates?|list (?:all|every|each)|extract|describe (?:the|this|each|all|what)|"
    r"score (?:\d|each|it|them)|rate (?:each|it|them|\d)|identify (?:the|all|each|which)|what (?:is|are) the|"
    r"which (?:one|frame|tile|of)|how many|count)\b", re.I)
_PASSFAIL = re.compile(r"\b(pass\s*(?:/|or|\|)\s*fail|pass-fail|PASS\b[^.?!\n]{0,40}\bFAIL|verdict)\b", re.I)
_YESNO_LEAD = re.compile(r"^\W*(is|are|does|do|did|can|could|has|have|was|were|should|will|would)\b", re.I)
_YESNO_ANY = re.compile(r"\b(yes\s*(?:/|or)\s*no|answer (?:only )?yes|confirm (?:whether|if)|whether)\b", re.I)
_QA = re.compile(r"\b(qa|quality (?:check|gate)|visual audit|gate this|inspect (?:for|this)|check (?:for|that)|verify|"
                 r"confirm (?:that|there|no)|any (?:visible )?(?:clipping|overlap|overflow|defects?|artifacts?|errors?))\b", re.I)


def question_shape(question: Any) -> str:
    """``passfail`` | ``yesno`` | ``qa`` | ``open``. Only the first three are sent to the model."""
    text = str(question or "").strip()
    if not text or _OPEN.search(text):
        return "open"
    if _PASSFAIL.search(text):
        return "passfail"
    sentences = [s for s in re.split(r"(?<=[.?!])\s+", text) if s.strip()]
    # "Strict QA for the tile: is the logo clipped?" asks the clause after the colon
    asks = [re.split(r"[:;]\s+|\s[-\u2013\u2014]\s", s)[-1] for s in sentences if s.rstrip().endswith("?")]
    if (len(asks) == 1 and _YESNO_LEAD.search(asks[0])) or (len(sentences) == 1 and _YESNO_LEAD.search(text)) \
            or _YESNO_ANY.search(text):
        return "yesno"
    if _QA.search(text):
        return "qa"
    return "open"


def omni_question(question: str, shape: str) -> str:
    text = " ".join(str(question or "").split())[:MAX_QUESTION_CHARS]
    if shape == "qa":
        return f"Visual quality check: {text} Does the image pass this check?"
    return text


# ── the current model's answer, as a class ───────────────────────────────────

_LABELLED_PF = re.compile(r"(?:overall|verdict|final|result|judg(?:e)?ment|conclusion|status|gate|qa)\W{0,6}"
                          r"(?:is\W{0,3}|=\W{0,3})?\**\W{0,3}(PASS|FAIL)(?:ED|ES)?\b", re.I)
_BARE_PF = re.compile(r"\b(PASS|FAIL)(?:ED|ES)?\b")
_LEAD_YN = re.compile(r"^\W*(yes|no)\b", re.I)
_LABELLED_YN = re.compile(r"\b(?:answer|verdict|conclusion|in short|so|therefore)\W{0,6}\**\W{0,3}(yes|no)\b",
                          re.I)
_BOLD_YN = re.compile(r"\*\*\s*(yes|no)\b", re.I)


def current_answer(result: Any) -> Tuple[str, Optional[str], int]:
    """``(path, text, chars)``: ``aux`` with the helper model's text, ``native``, or ``error``."""
    if isinstance(result, dict):
        return ("native" if result.get("_multimodal") else "error"), None, 0
    try:
        data = json.loads(result) if isinstance(result, str) else None
    except (TypeError, ValueError):
        data = None
    if not isinstance(data, dict):
        return "error", None, 0
    analysis = data.get("analysis")
    if not data.get("success") or not isinstance(analysis, str):
        return "error", None, 0
    return "aux", analysis, len(analysis)


def answer_class(text: Optional[str], shape: str) -> Optional[str]:
    """PASS / FAIL / Yes / No from the helper model's prose, or ``unclear``; None when there is no text."""
    if text is None:
        return None
    if shape in ("passfail", "qa"):
        labelled = _LABELLED_PF.findall(text)
        if labelled:
            return labelled[-1].upper()
        bare = {m.upper() for m in _BARE_PF.findall(text)}
        return bare.pop() if len(bare) == 1 else "unclear"
    for pattern in (_LEAD_YN, _LABELLED_YN, _BOLD_YN):
        found = pattern.findall(text)
        if found:
            return found[-1 if pattern is not _LEAD_YN else 0].capitalize()
    return "unclear"


# ── inside the hook: cheap, no file reads ────────────────────────────────────

def snapshot(tool_name: str, args: Any, result: Any, ids: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """What the shadow job needs, captured at once. None when this call is not ours."""
    if tool_name not in VISION_TOOLS or not isinstance(args, dict):
        return None
    image, question = args.get("image_url"), args.get("question")
    if not isinstance(image, str) or not image.strip():
        return None
    payload: Dict[str, Any] = {"tool": tool_name, "image": image.strip(), "question": str(question or ""),
                               "region": args.get("region"), "session_id": str(ids.get("session_id") or ""),
                               "tool_call_id": ids.get("tool_call_id"), "turn_id": ids.get("turn_id")}
    path, text, chars = current_answer(result)
    payload.update({"current_path": path, "current_text": text, "current_chars": chars})
    if not image.startswith(("data:", "http://", "https://")):
        try:
            st = os.stat(os.path.expanduser(image))
            payload["stat"] = (st.st_size, st.st_mtime_ns)
        except OSError:
            payload["stat"] = None
    return payload


# ── on the shadow thread ─────────────────────────────────────────────────────

def omni_dir() -> Path:
    return Path(os.environ.get("JEV_OMNI_DIR") or Path.home() / "Projects" / "jev-omni-local").expanduser()


def omni_python() -> Path:
    override = os.environ.get("JEV_OMNI_PYTHON")
    return Path(override).expanduser() if override else omni_dir() / "venv" / "bin" / "python"


def log_path() -> Path:
    return ledger.hermes_home() / "logs" / "jev-vision.jsonl"


def _digest(text: Any, n: int = 16) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()[:n]


def _append(row: Dict[str, Any]) -> None:
    try:
        target = log_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        entry = {"ts": round(time.time(), 3), "profile": ledger.profile(), "kind": "vision", **row}
        fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, separators=(",", ":"), default=str) + "\n")
    except OSError:
        pass


def available_gb() -> Optional[float]:
    """Free + inactive + purgeable memory on macOS (``vm_stat``); None when it cannot be read."""
    try:
        out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=2).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    page = re.search(r"page size of (\d+) bytes", out)
    pages = {m.group(1): int(m.group(2)) for m in re.finditer(r"Pages (free|inactive|purgeable|speculative):\s+(\d+)", out)}
    if not page or "free" not in pages:
        return None
    return round(sum(pages.values()) * int(page.group(1)) / 1e9, 1)


def _materialize(image: str) -> Tuple[Optional[Path], Optional[str], bool]:
    """``(path, skip_reason, is_temp)``. Remote URLs are not fetched: the shadow only reads what is on disk."""
    if image.startswith(("http://", "https://")):
        return None, "remote_url", False
    if image.startswith("data:"):
        try:
            header, encoded = image.split(",", 1)
            if ";base64" not in header or len(encoded) > MAX_IMAGE_BYTES * 4 // 3 + 4:
                return None, "data_url_unsupported", False
            raw = base64.b64decode(encoded, validate=False)
        except (ValueError, binascii.Error):
            return None, "data_url_unsupported", False
        handle = tempfile.NamedTemporaryFile(prefix="jev-vision-", suffix=".img", delete=False)
        with handle:
            handle.write(raw)
        return Path(handle.name), None, True
    path = Path(os.path.expanduser(image))
    if not path.is_absolute():
        return None, "relative_path", False
    if not path.is_file():
        return None, "image_gone", False
    if path.stat().st_size > MAX_IMAGE_BYTES:
        return None, "image_too_large", False
    return path, None, False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def run_omni(items: list, timeout: float = TIMEOUT_S) -> Dict[str, Any]:
    """One subprocess in the Jev-Omni environment: ``{"load_s", "results": [...]}`` or ``{"error"}``.

    Offline flags are set so the model libraries never reach for the network.
    """
    python = omni_python()
    if not python.is_file():
        return {"error": "omni_not_installed"}
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", ""),
           "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_DATASETS_OFFLINE": "1",
           "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        proc = subprocess.run([str(python), str(RUNNER), "--omni-dir", str(omni_dir())],
                              input=json.dumps({"items": items}), capture_output=True, text=True,
                              timeout=timeout, env=env, cwd=str(omni_dir()))
    except subprocess.TimeoutExpired:
        return {"error": "timeout"}
    except OSError as error:
        return {"error": f"spawn_failed:{type(error).__name__}"}
    lines = [line for line in (proc.stdout or "").splitlines() if line.startswith("{")]
    if proc.returncode != 0 or not lines:
        return {"error": f"runner_exit_{proc.returncode}"}
    try:
        return json.loads(lines[-1])
    except ValueError:
        return {"error": "runner_bad_output"}


class _MachineLock:
    """An flock on ``<hermes root>/jev/vision-omni.lock``: one model load at a time machine-wide."""

    def __init__(self, path: Path, wait: float) -> None:
        self.path, self.wait, self.handle = path, wait, None

    def __enter__(self) -> bool:
        if fcntl is None:
            return True
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.handle = open(self.path, "a")
        except OSError:
            return True  # a lock we cannot make must not stop the shadow; the memory guard still runs
        deadline = time.monotonic() + self.wait
        while True:
            try:
                fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except OSError:
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.25)

    def __exit__(self, *exc: Any) -> None:
        if self.handle is not None:
            try:
                fcntl.flock(self.handle, fcntl.LOCK_UN)
            finally:
                self.handle.close()


def job(payload: Dict[str, Any], queued: float, mode: Callable[[], str], dropped: Callable[[], int],
        lock_path: Path, runner: Optional[Callable[..., Dict[str, Any]]] = None) -> Optional[Dict[str, Any]]:
    """Run the hybrid's local half on one call and log one row. Returns the row (tests)."""
    if mode() != "shadow":
        return None
    runner = runner or run_omni
    shape = question_shape(payload.get("question"))
    current = answer_class(payload.get("current_text"), shape) if shape != "open" else None
    row: Dict[str, Any] = {
        "tool": payload.get("tool"), "session": _digest(payload.get("session_id")),
        "tool_call_id": payload.get("tool_call_id"), "turn_id": payload.get("turn_id"),
        "question_sha": _digest(payload.get("question")), "shape": shape, "region": bool(payload.get("region")),
        "current_path": payload.get("current_path"), "current_class": current,
        "current_chars": payload.get("current_chars"), "queued_ms": int((time.monotonic() - queued) * 1000),
        "queue_dropped": dropped(), "threshold": THRESHOLD, "model": MODEL_NAME}

    def finish(status: str, **more: Any) -> Dict[str, Any]:
        row.update(status=status, **more)
        _append(row)
        if status in ("ok", "error"):
            local = bool(row.get("handled_locally"))
            ledger.append({"feature": FEATURE, "mode": "shadow", "jev_model": MODEL_NAME, "provider": "local",
                           "n_questions": 1, "input_tokens": 0, "cost_usd": 0.0, "list_usd": 0.0,
                           "latency_ms": row.get("wall_ms"), "status": status, "source": "local",
                           "error": row.get("error"), "action": row.get("omni_class"), "shadow": True,
                           "fallback_used": status != "ok" or not local, "queue_dropped": row["queue_dropped"],
                           # the helper model's prose the agent would not have read: an estimate, ~4 chars a token
                           "llm_avoided_est": int((row.get("current_chars") or 0) / 4) if local else 0})
        return row

    if shape == "open":
        return finish("skipped", reason="open_question")
    image = str(payload.get("image") or "")
    path, reason, temp = _materialize(image)
    if path is None:
        return finish("skipped", reason=reason)
    try:
        if not temp and payload.get("stat") is not None:
            st = path.stat()
            if (st.st_size, st.st_mtime_ns) != tuple(payload["stat"]):
                return finish("skipped", reason="image_changed")
        row["image_sha256"] = _sha256(path)
        free = available_gb()
        row["free_gb"] = free
        if free is not None and free < MIN_FREE_GB:
            return finish("skipped", reason="low_memory")
        with _MachineLock(lock_path, LOCK_WAIT_S) as got:
            if not got:
                return finish("skipped", reason="busy")
            options = OPTIONS[shape]
            started = time.monotonic()
            out = runner([{"id": "1", "image": str(path), "state": STATE,
                           "question": omni_question(payload.get("question") or "", shape),
                           "options": list(options), "region": payload.get("region")}])
            wall_ms = int((time.monotonic() - started) * 1000)
        results = out.get("results") or []
        first = results[0] if results else {}
        if out.get("error") or first.get("error") or first.get("prediction") not in options:
            return finish("error", wall_ms=wall_ms,
                          error=str(out.get("error") or first.get("error") or "no_prediction")[:80])
        answer, confidence = first["prediction"], float(first.get("confidence") or 0.0)
        local = confidence >= THRESHOLD
        agree = None if current in (None, "unclear") else (answer.lower() == current.lower())
        return finish("ok", omni_class=answer, omni_confidence=round(confidence, 4),
                      handled_locally=local, agree=agree,
                      hybrid_class=answer if local else current,
                      omni_ms=first.get("ms"), load_s=out.get("load_s"), peak_gb=first.get("peak_gb"),
                      wall_ms=wall_ms)
    finally:
        if temp:
            try:
                path.unlink()
            except OSError:
                pass
