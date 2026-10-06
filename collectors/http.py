"""requests.get with a hard wall-clock deadline.

requests' timeouts bound individual socket operations, not the whole
request, so a stalled or trickling connection can hold a call open for many
minutes. The request runs in a daemon thread; if it hasn't finished by the
deadline we raise Timeout and move on (the abandoned thread dies on its own
socket timeout and never blocks process exit).
"""
import threading

import requests


def get(url, *, deadline_seconds, params=None, headers=None, connect_timeout=5, read_timeout=10, allow_redirects=True):
    outcome = {}

    def _run():
        try:
            outcome["response"] = requests.get(
                url, params=params, headers=headers,
                timeout=(connect_timeout, read_timeout), allow_redirects=allow_redirects,
            )
        except BaseException as exc:  # noqa: BLE001 - re-raised in caller thread
            outcome["error"] = exc

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join(deadline_seconds)
    if worker.is_alive():
        raise requests.Timeout(f"exceeded {deadline_seconds}s total request deadline")
    if "error" in outcome:
        raise outcome["error"]
    return outcome["response"]
