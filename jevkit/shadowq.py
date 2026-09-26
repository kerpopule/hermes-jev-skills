"""Shadow work off the hot path: one daemon thread per process, a bounded queue, drops counted.

A shadow decision must never make the thing it watches slower. Hermes runs
``pre_approval_request``/``post_approval_response`` synchronously with no time limit, and a
``pre_tool_call`` that overruns its budget blocks the tool, so a hook that waited on Jev would
turn a TypeSafe hiccup into a stalled agent. A hook calls ``submit`` and returns; the thread
does the call later. When the queue is full the OLDEST job is dropped (the newest event is
the one a reader of the log is most likely to look for) and the drop is counted, so a report
can say how much of the shadow it never saw.
"""
from __future__ import annotations

import collections
import threading
from typing import Any, Callable, Deque, Dict, Optional, Tuple

MAX_JOBS = 256


class ShadowQueue:
    def __init__(self, maxsize: int = MAX_JOBS) -> None:
        self._jobs: Deque[Tuple[Callable[..., Any], tuple, dict]] = collections.deque()
        self._maxsize = maxsize
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._thread: Optional[threading.Thread] = None
        self.dropped = 0
        self.done = 0
        self.failed = 0
        self._running = 0

    def submit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> bool:
        """Queue a job and return at once. False when an older job had to be dropped for it."""
        with self._lock:
            dropped = False
            while len(self._jobs) >= self._maxsize:
                self._jobs.popleft()
                self.dropped += 1
                dropped = True
            self._jobs.append((fn, args, kwargs))
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="jev-shadow", daemon=True)
                self._thread.start()
            self._wake.notify()
        return not dropped

    def _run(self) -> None:
        while True:
            with self._lock:
                while not self._jobs:
                    self._wake.wait()
                fn, args, kwargs = self._jobs.popleft()
                self._running += 1
            try:
                fn(*args, **kwargs)
                ok = True
            except Exception:  # noqa: BLE001 - a shadow job must never take the thread down
                ok = False
            with self._lock:
                self._running -= 1
                if ok:
                    self.done += 1
                else:
                    self.failed += 1

    def pending(self) -> int:
        with self._lock:
            return len(self._jobs)

    def stats(self) -> Dict[str, int]:
        with self._lock:
            return {"pending": len(self._jobs), "dropped": self.dropped, "done": self.done,
                    "failed": self.failed}

    def drain(self, timeout: float = 5.0) -> bool:
        """Wait until the queue is empty (tests and short-lived CLIs). True when it emptied."""
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if not self._jobs and not self._running:
                    return True
            time.sleep(0.005)
        return False


_QUEUE = ShadowQueue()


def submit(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> bool:
    return _QUEUE.submit(fn, *args, **kwargs)


def stats() -> Dict[str, int]:
    return _QUEUE.stats()


def dropped() -> int:
    return _QUEUE.dropped
