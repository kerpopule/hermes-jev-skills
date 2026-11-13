"""Bound optional pre-call work without queueing or applying late results.

The callback must return data only: never publish turn state or feedback from it.
Provider transports retain their own timeouts. Already-running requests cannot be
cancelled by Python; at most six outstanding callbacks are admitted, without a queue.
"""
import contextvars
import threading
from typing import Any, Callable, Tuple

_SLOTS = threading.BoundedSemaphore(6)
WAIT_SECONDS = 1.5


def run(callback: Callable[[], Any], timeout: float = WAIT_SECONDS,
        slots: Any = None) -> Tuple[str, Any]:
    admission = _SLOTS if slots is None else slots
    if not admission.acquire(blocking=False):
        return 'busy', None
    context = contextvars.copy_context()
    ready = threading.Event()
    answer = []

    def work():
        try:
            answer.append(('ok', context.run(callback)))
        except Exception:
            answer.append(('error', None))
        finally:
            admission.release()
            ready.set()

    try:
        threading.Thread(target=work, name='jev-preflight', daemon=True).start()
    except Exception:
        admission.release()
        return 'error', None
    if not ready.wait(timeout):
        return 'timeout', None
    return answer[0]
