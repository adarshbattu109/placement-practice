"""The grading worker: one background thread that transcribes, then judges.

ASR is ~3s and the judge is 20-60s, so a 21-question sitting would keep the candidate on a
spinner for twenty minutes. `/submit` now stores the take and queues it; this drains the
queue and the browser polls. Nothing here touches HTTP — it reads and writes the same
tables the routes do.
"""

import logging
import threading
import time

from app.evaluate import _client, evaluate
from src.constants.constants import (
    ASR_MODEL,
    ASR_OPTIONS,
    JOB_STALE_MINUTES,
    RECORDING_RETENTION_DAYS,
    WORKER_POLL_SECONDS,
)
from src.db import store

log = logging.getLogger("app.worker")

_stop = threading.Event()


def start():
    """Run the queue in a daemon thread. Idempotent — a second call is a no-op.

    In-process rather than a separate command: the queue is a table in the same file, so a
    thread is the whole of what a job runner needs at a few jobs a minute. ponytail: one
    worker in the web process, so uvicorn --workers 2 would run two of them — harmless,
    because claiming is atomic, but split it out before scaling the web tier.
    """
    if any(t.name == "grader" for t in threading.enumerate()):
        return
    _stop.clear()  # a previous stop() must not make the next start a no-op
    threading.Thread(target=_run, name="grader", daemon=True).start()
    log.info("grading worker started, polling every %ds", WORKER_POLL_SECONDS)


def stop():
    """Ask the thread to finish its current job and exit. For tests and a clean shutdown."""
    _stop.set()


def _run():
    while not _stop.is_set():
        try:
            if not step():
                # Nothing waiting, so this is where the housekeeping goes. The watchdog
                # only matters when the queue looks empty: a job stuck in 'running' is
                # exactly what makes it look that way.
                if freed := store.requeue_stale_jobs():
                    log.warning(
                        "re-queued %d job(s) claimed over %dm ago", freed, JOB_STALE_MINUTES
                    )
                _sweep()
                _stop.wait(WORKER_POLL_SECONDS)
        except Exception:
            # The thread is the only thing draining the queue; it must not die on a
            # surprise. Individual job failures are handled in step().
            log.exception("worker loop error, continuing")
            _stop.wait(WORKER_POLL_SECONDS)


_swept_at = 0.0


def _sweep():
    """Delete recordings past the retention window, at most once an hour.

    Rate-limited because the idle loop runs every few seconds and the sweep is a table
    scan. It used to run at boot only, which meant a server left up for a fortnight kept
    everything it had captured.
    """
    global _swept_at
    if time.monotonic() - _swept_at < 3600:
        return
    _swept_at = time.monotonic()
    if gone := store.prune_recordings():
        log.info("pruned %d recording(s) past the %d-day window", gone, RECORDING_RETENTION_DAYS)


def step():
    """Claim and run one job. Returns False when the queue is empty.

    Split out from the loop so a test can drive the queue without a thread or a clock.
    """
    job = store.claim_job()
    if job is None:
        return False

    stage = _transcribe if job["kind"] == "transcribe" else _grade
    started = time.perf_counter()
    try:
        stage(job)
    except Exception as e:
        # Every failure mode here is remote and transient — a 502, a rate limit, a
        # depleted balance — so the job goes back to the queue rather than up the stack.
        # Logged with the traceback because nothing else will ever see this exception.
        outcome = store.fail_job(job["id"], e)
        log.exception(
            "job %d (%s %s) attempt %d failed, now %s",
            job["id"],
            job["kind"],
            job["question_id"],
            job["attempts"],
            outcome["status"],
        )
    else:
        log.info(
            "job %d %s %s in %.1fs",
            job["id"],
            job["kind"],
            job["question_id"],
            time.perf_counter() - started,
        )
    _report_ready(job["assessment_id"])
    return True


def _transcribe(job):
    """ASR on the stored take, then hand the job to the judge.

    Reads the Opus back out rather than keeping the WAV in memory across the request: the
    take is already the durable copy, and a restart between stages must not lose it.
    """
    take = store.recording(job["assessment_id"], job["question_id"])
    if take is None:
        raise RuntimeError("no recording stored for this answer")

    result = _client().automatic_speech_recognition(
        take["audio"], model=ASR_MODEL, extra_body=ASR_OPTIONS
    )
    transcript = result.text.strip()
    log.info("asr %s — %d words", job["question_id"], len(transcript.split()))

    store.save_answer(job["assessment_id"], job["question_id"], transcript, take["seconds"] or 0.0)
    store.advance_job(job["id"], "grade")


def _grade(job):
    """Judge the stored transcript and attach the verdict."""
    ready = store.job_input(job["assessment_id"], job["question_id"])
    if ready is None or ready["transcript"] is None:
        raise RuntimeError("no transcript to grade")

    report = evaluate(ready["transcript"], ready["text"], ready["ideal_answer"], ready["level"])
    store.grade_answer(job["assessment_id"], job["question_id"], report)
    store.rescore_assessment(job["assessment_id"])
    store.finish_job(job["id"])


def _report_ready(assessment_id):
    """Log once when a sitting has no work left. The candidate sees it by polling.

    ponytail: no email. The spec's mailer needs a sending domain with SPF/DKIM/DMARC, and
    the status page already removes the wait it was there to solve. Slot the send in here
    when there is a domain to send from.
    """
    if store.assessment_jobs_left(assessment_id) == 0:
        log.info("assessment %d fully graded", assessment_id)


def demo():
    """Self-check: both stages run off the queue, and a failure retries then parks.

    ASR and the judge are stubbed — the point is the queue mechanics, not the models.
    """
    import pathlib
    import tempfile

    global _client, evaluate

    with tempfile.TemporaryDirectory() as tmp:
        db = pathlib.Path(tmp) / "w.db"
        store.DB_PATH = db
        store.setup(db)
        uid = store.create_user("w@example.com", "W", "x" * 12, path=db)
        qs = store.questions(path=db)[:2]
        sitting = store.start_assessment(uid, None, "beginner", 2, path=db)
        for q in qs:
            store.save_recording(sitting, q["id"], b"opus bytes", 20.0, path=db)
            store.enqueue(sitting, q["id"], path=db)

        heard = "a hash map hashes the key to a bucket and collisions chain in that bucket"
        real_client, real_evaluate = _client, evaluate
        _client = lambda: type(
            "Stub",
            (),
            {
                "automatic_speech_recognition": lambda self, audio, **kw: type(
                    "R", (), {"text": heard}
                )()
            },
        )()
        evaluate = lambda t, q, i, level: {"score": 64, "parameters": {}, "comments": {}}
        try:
            # Two jobs, two stages each: four steps, then the queue is empty.
            assert [step() for _ in range(5)] == [True, True, True, True, False]
        finally:
            _client, evaluate = real_client, real_evaluate

        assert store.assessment_jobs_left(sitting, path=db) == 0
        full = store.assessment(uid, sitting, path=db)
        assert full["overall"] == 64, full["overall"]
        assert len(full["answers"]) == 2 and full["jobs"] == [], full
        assert all(a["transcript"] == heard and a["score"] == 64 for a in full["answers"]), full

        # A take that was never stored is the one failure the worker itself raises, and it
        # must park rather than spin. The traceback is the point of the code under test, so
        # it is silenced here rather than printed three times into a passing run.
        store.save_answer(sitting, qs[0]["id"], heard, 20.0, path=db)
        with store.connect(db) as conn:
            conn.execute("DELETE FROM recordings")
        store.enqueue(sitting, qs[0]["id"], path=db)
        log.setLevel(logging.CRITICAL)
        try:
            while step():
                pass
        finally:
            log.setLevel(logging.NOTSET)
        stuck = store.assessment(uid, sitting, path=db)["jobs"]
        assert [j["status"] for j in stuck] == ["failed"], stuck
        assert "no recording" in stuck[0]["error"], stuck

    print(f"ok — queue drains through both stages, overall={full['overall']}, failures park")


if __name__ == "__main__":
    demo()
