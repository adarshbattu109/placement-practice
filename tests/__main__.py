"""Run every check before a push: `uv run python -m tests`.

Exits non-zero on the first failure so CI and a pre-push hook both read it correctly.
The module self-checks come first — they are the fastest and the most specific, so a
broken parser or a broken cookie is reported before a route test buries it.
"""

import sys
import time
import traceback

import app.evaluate
import app.worker
import src.db.store
import src.helpers.inference
import src.helpers.security
import src.helpers.speech
from tests import test_api, test_frontend, test_packaging

# ponytail: hand-listed, add a check here when you add one. Discovery is not worth
# importlib for a list this short.
CHECKS = (
    ("security", src.helpers.security.demo),
    ("inference", src.helpers.inference.demo),
    ("speech", src.helpers.speech.demo),
    ("store", src.db.store.demo),
    ("evaluate", app.evaluate.demo),
    # Before the route checks: both rebind store.DB_PATH, and the worker's runs on a temp
    # database that is gone by the time test_api points it at its own.
    ("worker", app.worker.demo),
    ("api", test_api.check),
    ("frontend", test_frontend.check),
    ("packaging", test_packaging.check),
)


def main(only=None):
    failed = []
    for name, run in CHECKS:
        if only and name not in only:
            continue
        started = time.perf_counter()
        print(f"── {name}")
        try:
            run()
        except Exception:  # noqa: BLE001 — the runner reports every failure, not just the first
            traceback.print_exc()
            failed.append(name)
        print(f"   {time.perf_counter() - started:.1f}s\n")

    if failed:
        print(f"FAILED: {', '.join(failed)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or None))
