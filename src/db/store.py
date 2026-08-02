"""Every SQL statement in the project. Nothing here knows HTTP exists.

Connections are opened per call rather than pooled: SQLite opens in microseconds, and
one connection per call means no thread-affinity rules for FastAPI's threadpool to break.
ponytail: connection per call, add a per-thread cache if profiling ever shows it matters.
"""

import contextlib
import json
import sqlite3

from src.constants.constants import (
    JOB_STALE_MINUTES,
    LEVELS,
    MAX_JOB_ATTEMPTS,
    RECORDING_RETENTION_DAYS,
)
from src.constants.filepaths import DB_PATH, QUESTIONS_JSON, SCHEMA_SQL
from src.helpers.security import hash_password, verify_password


@contextlib.contextmanager
def connect(path=None):
    """A configured connection that commits on success and rolls back on any exception."""
    conn = sqlite3.connect(path or DB_PATH, isolation_level="DEFERRED")
    conn.row_factory = sqlite3.Row
    # WAL lets the judge worker write while a request reads. foreign_keys is off by
    # default in SQLite, so the ON DELETE CASCADEs in schema.sql are inert without it.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def setup(path=None):
    """Apply the schema and seed the question bank on a fresh database."""
    (path or DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as conn:
        conn.executescript(SCHEMA_SQL.read_text())
        if not conn.execute("SELECT 1 FROM questions LIMIT 1").fetchone():
            _seed(conn, json.loads(QUESTIONS_JSON.read_text()))


def _seed(conn, bank):
    """Load questions.json into topics + questions. The JSON stays the seed, not the source."""
    for question in bank:
        conn.execute("INSERT OR IGNORE INTO topics(name) VALUES (?)", (question["topic"],))
        conn.execute(
            "INSERT INTO questions(id, topic_id, text, ideal_answer) "
            "VALUES (?, (SELECT id FROM topics WHERE name = ?), ?, ?)",
            (question["id"], question["topic"], question["text"], question["ideal_answer"]),
        )


# --- questions ----------------------------------------------------------------------


def topics(path=None):
    """Every topic name that has at least one question, alphabetically."""
    with connect(path) as conn:
        rows = conn.execute(
            "SELECT t.name FROM topics t JOIN questions q ON q.topic_id = t.id "
            "GROUP BY t.id ORDER BY t.name"
        ).fetchall()
    return [row["name"] for row in rows]


def questions(topic=None, path=None):
    """Questions for one topic, or all of them. Excludes ideal_answer — the browser sees these."""
    with connect(path) as conn:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT q.id, t.name AS topic, q.text FROM questions q "
                "JOIN topics t ON t.id = q.topic_id "
                "WHERE ?1 IS NULL OR t.name = ?1 ORDER BY q.id",
                (topic,),
            )
        ]


def question(question_id, path=None):
    """One question including its ideal answer, or None. Server-side only."""
    with connect(path) as conn:
        row = conn.execute(
            "SELECT q.id, t.name AS topic, q.text, q.ideal_answer FROM questions q "
            "JOIN topics t ON t.id = q.topic_id WHERE q.id = ?",
            (question_id,),
        ).fetchone()
    return dict(row) if row else None


# --- accounts -----------------------------------------------------------------------


def create_user(email, name, password, level="advanced", path=None):
    """Create a user and profile. Returns the new id, or None if the email is taken.

    Hashing happens outside the transaction because scrypt takes ~22ms and holding a
    write lock for it would serialise every other writer behind a signup.
    """
    email = email.strip().lower()
    digest = hash_password(password)
    if level not in LEVELS:
        raise ValueError(f"Unknown level {level!r}. Expected one of {sorted(LEVELS)}.")
    try:
        with connect(path) as conn:
            cursor = conn.execute(
                "INSERT INTO users(email, password_hash) VALUES (?, ?)", (email, digest)
            )
            conn.execute(
                "INSERT INTO profiles(user_id, name, default_level) VALUES (?, ?, ?)",
                (cursor.lastrowid, name.strip(), level),
            )
            return cursor.lastrowid
    except sqlite3.IntegrityError:
        return None  # email UNIQUE — a taken address is an expected outcome, not a fault


def authenticate(email, password, path=None):
    """Return the user id if the password matches, else None.

    A missing email still runs a hash so a wrong address and a wrong password take the
    same time — the response must not tell an attacker which accounts exist.
    """
    with connect(path) as conn:
        row = conn.execute(
            "SELECT id, password_hash FROM users WHERE email = ?", (email.strip().lower(),)
        ).fetchone()
    stored = row["password_hash"] if row else hash_password("timing-equaliser")
    return row["id"] if verify_password(password, stored) and row else None


def profile(user_id, path=None):
    """The signed-in user's email, name and default level, or None if the id is stale."""
    with connect(path) as conn:
        row = conn.execute(
            "SELECT u.id, u.email, p.name, p.default_level FROM users u "
            "JOIN profiles p ON p.user_id = u.id WHERE u.id = ?",
            (user_id,),
        ).fetchone()
    return dict(row) if row else None


# --- assessments --------------------------------------------------------------------


def start_assessment(user_id, topic, level, questions, path=None):
    """Open a sitting and return its id. `topic` is None for every topic."""
    if level not in LEVELS:
        raise ValueError(f"Unknown level {level!r}. Expected one of {sorted(LEVELS)}.")
    with connect(path) as conn:
        return conn.execute(
            "INSERT INTO assessments(user_id, topic, level, questions) VALUES (?, ?, ?, ?)",
            (user_id, topic, level, questions),
        ).lastrowid


def owns_assessment(user_id, assessment_id, path=None):
    """Whether this sitting belongs to this user, and is still open.

    Every write path checks this. Without it, passing someone else's assessment_id would
    file answers against their interview.
    """
    with connect(path) as conn:
        row = conn.execute(
            "SELECT 1 FROM assessments WHERE id = ? AND user_id = ? AND finished_at IS NULL",
            (assessment_id, user_id),
        ).fetchone()
    return row is not None


def save_answer(assessment_id, question_id, transcript, seconds, report=None, path=None):
    """Record one answer, replacing any earlier take of the same question."""
    with connect(path) as conn:
        return conn.execute(
            "INSERT INTO answers(assessment_id, question_id, transcript, seconds, report, score) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(assessment_id, question_id) DO UPDATE SET "
            "  transcript = excluded.transcript, seconds = excluded.seconds,"
            "  report = excluded.report, score = excluded.score,"
            "  created_at = datetime('now')",
            (
                assessment_id,
                question_id,
                transcript,
                seconds,
                json.dumps(report) if report is not None else None,
                (report or {}).get("score"),
            ),
        ).lastrowid


def finish_assessment(assessment_id, path=None):
    """Close a sitting, averaging its graded answers into `overall`. Returns that average.

    Ungraded answers are excluded from the average rather than counted as zero: a 502 from
    the judge is not the candidate's fault. Unanswered questions never reach `answers`.
    Grading now runs after this, so `overall` starts NULL and `rescore_assessment()` fills
    it in as verdicts land.
    """
    with connect(path) as conn:
        conn.execute(
            "UPDATE assessments SET finished_at = datetime('now'),"
            "  overall = (SELECT round(avg(score)) FROM answers"
            "             WHERE assessment_id = ? AND score IS NOT NULL)"
            " WHERE id = ? AND finished_at IS NULL",
            (assessment_id, assessment_id),
        )
        row = conn.execute(
            "SELECT overall FROM assessments WHERE id = ?", (assessment_id,)
        ).fetchone()
    return row["overall"] if row else None


def rescore_assessment(assessment_id, path=None):
    """Recompute `overall` from whatever is graded so far. Returns the new average.

    Separate from finish_assessment because that one closes the sitting and must stay a
    no-op on a second call, whereas the worker calls this after every verdict — the average
    climbs as answers land rather than appearing all at once at the end.
    """
    with connect(path) as conn:
        conn.execute(
            "UPDATE assessments SET overall = (SELECT round(avg(score)) FROM answers"
            "  WHERE assessment_id = ? AND score IS NOT NULL) WHERE id = ?",
            (assessment_id, assessment_id),
        )
        row = conn.execute(
            "SELECT overall FROM assessments WHERE id = ?", (assessment_id,)
        ).fetchone()
    return row["overall"] if row else None


# Counts of outstanding and failed work, added to every sitting the browser is shown so it
# knows whether to keep polling. Correlated subqueries rather than another LEFT JOIN: the
# answers join is already fanning out the row, and a second one would multiply the counts.
_PROGRESS = (
    "(SELECT count(*) FROM jobs j WHERE j.assessment_id = a.id"
    "   AND j.status IN ('pending', 'running')) AS working,"
    " (SELECT count(*) FROM jobs j WHERE j.assessment_id = a.id"
    "   AND j.status = 'failed') AS stuck"
)


def history(user_id, limit=20, path=None):
    """A user's finished sittings, newest first, for the history page."""
    with connect(path) as conn:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT a.id, a.topic, a.level, a.questions, a.overall, a.started_at,"
                "       a.finished_at, count(n.id) AS answered,"
                f"      {_PROGRESS}"
                " FROM assessments a LEFT JOIN answers n ON n.assessment_id = a.id"
                " WHERE a.user_id = ? AND a.finished_at IS NOT NULL"
                " GROUP BY a.id ORDER BY a.started_at DESC LIMIT ?",
                (user_id, limit),
            )
        ]


def assessment(user_id, assessment_id, path=None):
    """One past sitting with every answer, or None if it is not this user's.

    Scoped by user_id in the query rather than checked afterwards, so a wrong id cannot
    read another candidate's transcripts.
    """
    with connect(path) as conn:
        head = conn.execute(
            "SELECT a.id, a.topic, a.level, a.questions, a.overall, a.started_at, a.finished_at,"
            f"      {_PROGRESS}"
            " FROM assessments a WHERE a.id = ? AND a.user_id = ?",
            (assessment_id, user_id),
        ).fetchone()
        if head is None:
            return None
        answers = conn.execute(
            "SELECT n.question_id, q.text, t.name AS topic, n.transcript, n.report, n.score,"
            "       n.seconds"
            " FROM answers n JOIN questions q ON q.id = n.question_id"
            " JOIN topics t ON t.id = q.topic_id"
            " WHERE n.assessment_id = ? ORDER BY n.id",
            (assessment_id,),
        ).fetchall()
        # Outstanding work is listed separately because a take that has not been
        # transcribed yet has no `answers` row at all — without this the report page
        # would show a question as simply absent rather than as still in the queue.
        pending = conn.execute(
            "SELECT j.question_id, q.text, j.kind, j.status, j.error, j.attempts"
            " FROM jobs j JOIN questions q ON q.id = j.question_id"
            " WHERE j.assessment_id = ? AND j.status != 'done' ORDER BY j.id",
            (assessment_id,),
        ).fetchall()

    return dict(head) | {
        "answers": [
            dict(row) | {"report": json.loads(row["report"]) if row["report"] else None}
            for row in answers
        ],
        "jobs": [dict(row) for row in pending],
    }


# --- the grading queue ----------------------------------------------------------------
# Claiming is a single UPDATE ... RETURNING, which SQLite runs as one write transaction —
# so two workers cannot take the same row without SKIP LOCKED. Verified with 4 concurrent
# workers racing for 100 jobs: 100 claims, 100 distinct.


def enqueue(assessment_id, question_id, kind="transcribe", path=None):
    """Queue one take for the worker, resetting any earlier job for the same question.

    One row per question per sitting, mirroring answers. A re-submit of the same open
    sitting is a retry, not a second answer, so it restarts the work rather than queueing
    a duplicate job to race the first. A finished sitting takes no submits at all.
    """
    with connect(path) as conn:
        return conn.execute(
            "INSERT INTO jobs(assessment_id, question_id, kind) VALUES (?, ?, ?)"
            " ON CONFLICT(assessment_id, question_id) DO UPDATE SET"
            "   kind = excluded.kind, status = 'pending', attempts = 0, error = NULL,"
            "   claimed_at = NULL, created_at = datetime('now')",
            (assessment_id, question_id, kind),
        ).lastrowid


def claim_job(path=None):
    """Take the oldest waiting job, or None. Atomic — two workers cannot get the same row."""
    with connect(path) as conn:
        row = conn.execute(
            "UPDATE jobs SET status = 'running', attempts = attempts + 1,"
            "  claimed_at = datetime('now')"
            " WHERE id = (SELECT id FROM jobs WHERE status = 'pending' ORDER BY id LIMIT 1)"
            " RETURNING id, assessment_id, question_id, kind, attempts",
        ).fetchone()
    return dict(row) if row else None


def advance_job(job_id, kind, path=None):
    """Move a claimed job to its next stage and put it back in the queue.

    attempts resets: a flaky ASR call must not spend the judge's retries.
    """
    with connect(path) as conn:
        conn.execute(
            "UPDATE jobs SET kind = ?, status = 'pending', attempts = 0, claimed_at = NULL"
            " WHERE id = ?",
            (kind, job_id),
        )


def finish_job(job_id, path=None):
    """Mark a job done. Nothing deletes it — the row is the record of what was graded."""
    with connect(path) as conn:
        conn.execute(
            "UPDATE jobs SET status = 'done', error = NULL, claimed_at = NULL WHERE id = ?",
            (job_id,),
        )


def fail_job(job_id, error, max_attempts=MAX_JOB_ATTEMPTS, path=None):
    """Record a failure. Back to 'pending' if retries remain, else parked as 'failed'.

    The error text is kept either way, so a report can say what went wrong rather than
    showing a question that silently never scored.
    """
    with connect(path) as conn:
        row = conn.execute(
            "UPDATE jobs SET error = ?2, claimed_at = NULL,"
            "  status = CASE WHEN attempts >= ?3 THEN 'failed' ELSE 'pending' END"
            " WHERE id = ?1 RETURNING status, attempts",
            (job_id, str(error)[:500], max_attempts),
        ).fetchone()
    return dict(row) if row else None


def requeue_stale_jobs(minutes=JOB_STALE_MINUTES, path=None):
    """Return jobs claimed too long ago to the queue. Returns how many.

    A crash or a restart mid-job leaves the row 'running' with nobody working it, and
    nothing else would ever pick it up. ponytail: a time-based watchdog, not a worker
    heartbeat — a job genuinely slower than the window gets run twice.
    """
    with connect(path) as conn:
        return conn.execute(
            "UPDATE jobs SET status = 'pending', claimed_at = NULL"
            " WHERE status = 'running' AND claimed_at < datetime('now', ?)",
            (f"-{minutes} minutes",),
        ).rowcount


def assessment_jobs_left(assessment_id, path=None):
    """How many of a sitting's jobs are still unfinished — 0 means the report is ready."""
    with connect(path) as conn:
        return conn.execute(
            "SELECT count(*) c FROM jobs WHERE assessment_id = ?"
            "  AND status IN ('pending', 'running')",
            (assessment_id,),
        ).fetchone()["c"]


def job_input(assessment_id, question_id, path=None):
    """Everything the worker needs to run one stage, in one read.

    The level lives on the sitting rather than the job: it is what the whole interview was
    graded at, and a job that carried its own copy could disagree with the report header.
    """
    with connect(path) as conn:
        row = conn.execute(
            "SELECT a.level, q.text, q.ideal_answer, n.transcript"
            " FROM assessments a JOIN questions q ON q.id = ?2"
            " LEFT JOIN answers n ON n.assessment_id = a.id AND n.question_id = q.id"
            " WHERE a.id = ?1",
            (assessment_id, question_id),
        ).fetchone()
    return dict(row) if row else None


def grade_answer(assessment_id, question_id, report, path=None):
    """Attach a verdict to an answer that already has its transcript.

    Narrower than save_answer on purpose: the transcript is the expensive half and the
    judge stage must not be able to overwrite it with a stale copy.
    """
    with connect(path) as conn:
        return conn.execute(
            "UPDATE answers SET report = ?, score = ? WHERE assessment_id = ? AND question_id = ?",
            (json.dumps(report), report.get("score"), assessment_id, question_id),
        ).rowcount


# --- recordings ---------------------------------------------------------------------
# The bytes are already encoded by the caller: nothing under src/ chooses a codec.


def save_recording(assessment_id, question_id, audio, seconds, mime="audio/ogg", path=None):
    """Store one take, replacing any earlier take of the same question."""
    with connect(path) as conn:
        return conn.execute(
            "INSERT INTO recordings(assessment_id, question_id, audio, seconds, mime) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(assessment_id, question_id) DO UPDATE SET "
            "  audio = excluded.audio, seconds = excluded.seconds, mime = excluded.mime,"
            "  created_at = datetime('now')",
            (assessment_id, question_id, audio, seconds, mime),
        ).lastrowid


def recording(assessment_id, question_id, path=None):
    """One stored take, or None. `audio` materialises whole, so ask only when you need it."""
    with connect(path) as conn:
        row = conn.execute(
            "SELECT id, audio, mime, seconds, created_at FROM recordings"
            " WHERE assessment_id IS ?1 AND question_id = ?2",
            (assessment_id, question_id),
        ).fetchone()
    return dict(row) if row else None


def untranscribed(limit=20, path=None):
    """Takes with no answer row yet — what a re-run of ASR would work through.

    Deliberately excludes `audio`: this is the queue query, and pulling every blob into
    memory to list the queue would defeat the point of a separate table.
    """
    with connect(path) as conn:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT r.id, r.assessment_id, r.question_id, r.seconds, r.created_at"
                " FROM recordings r LEFT JOIN answers a"
                "   ON a.assessment_id IS r.assessment_id AND a.question_id = r.question_id"
                " WHERE a.id IS NULL ORDER BY r.created_at LIMIT ?",
                (limit,),
            )
        ]


def prune_recordings(days=RECORDING_RETENTION_DAYS, path=None):
    """Delete takes past the retention window. Returns how many went.

    Audio is the only thing here with an expiry: transcripts and scores are the point of
    the product, the recording is evidence with a shelf life.
    """
    with connect(path) as conn:
        return conn.execute(
            "DELETE FROM recordings WHERE created_at < datetime('now', ?)", (f"-{days} days",)
        ).rowcount


def demo():
    """Self-check against a throwaway database: seeding, signup, login, isolation."""
    import os
    import pathlib
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        db = pathlib.Path(tmp) / "t.db"
        setup(db)
        setup(db)  # idempotent: a second open must not double-seed

        bank = json.loads(QUESTIONS_JSON.read_text())
        assert len(questions(path=db)) == len(bank), "seed must load every question exactly once"
        assert topics(path=db) == sorted({q["topic"] for q in bank})
        one = questions(topic=topics(path=db)[0], path=db)
        assert one and all(q["topic"] == topics(path=db)[0] for q in one)
        assert questions(topic="Nope", path=db) == []
        assert "ideal_answer" not in one[0], "the browser must never receive ideal answers"
        assert question(one[0]["id"], path=db)["ideal_answer"]
        assert question("no-such-question", path=db) is None

        os.environ.setdefault("SECRET_KEY", "test-only-key")
        uid = create_user("Ada@Example.com ", " Ada ", "correct horse", path=db)
        assert uid and profile(uid, path=db)["email"] == "ada@example.com"  # normalised
        assert profile(uid, path=db)["name"] == "Ada"
        assert profile(uid, path=db)["default_level"] == "advanced"
        assert profile(uid + 999, path=db) is None

        assert create_user("ada@example.com", "Impostor", "x" * 12, path=db) is None
        # The rejected signup must not have left a profile row behind.
        assert len(questions(path=db)) == len(bank)
        with connect(db) as conn:
            assert conn.execute("SELECT count(*) c FROM profiles").fetchone()["c"] == 1

        assert authenticate("ADA@example.com", "correct horse", path=db) == uid
        assert authenticate("ada@example.com", "wrong", path=db) is None
        assert authenticate("nobody@example.com", "correct horse", path=db) is None
        try:
            create_user("b@example.com", "B", "x" * 12, level="guru", path=db)
            raise AssertionError("should have rejected an unknown level")
        except ValueError:
            pass

        _check_assessments(db, uid)
        _check_recordings(db, uid)
        _check_jobs(db, uid)

    print(
        f"ok — {len(bank)} questions seeded, signup/login round-trips, duplicates rejected, "
        "assessments persist and stay per-user, takes round-trip and expire, "
        "jobs claim once and retry then park"
    )


def _check_assessments(db, uid):
    """A whole sitting, end to end, plus the two ways it must not leak or double-count."""
    qs = questions(path=db)[:3]
    sitting = start_assessment(uid, "DSA", "expert", len(qs), path=db)
    assert owns_assessment(uid, sitting, path=db)

    verdict = {"score": 80, "parameters": {}, "comments": {"strengths": ["clear"]}}
    save_answer(sitting, qs[0]["id"], "first go", 30.0, verdict, path=db)
    save_answer(sitting, qs[1]["id"], "second", 25.0, {**verdict, "score": 60}, path=db)
    # Ungraded (the judge failed): stored, but must not drag the average down.
    save_answer(sitting, qs[2]["id"], "third", 10.0, None, path=db)

    # Re-recording replaces the take rather than adding a second row.
    save_answer(sitting, qs[0]["id"], "better answer", 40.0, {**verdict, "score": 90}, path=db)
    with connect(db) as conn:
        assert conn.execute("SELECT count(*) c FROM answers").fetchone()["c"] == 3

    assert finish_assessment(sitting, path=db) == 75, "avg(90, 60), ignoring the ungraded one"
    assert not owns_assessment(uid, sitting, path=db), "a finished sitting is closed to writes"
    # Finishing twice must not move the score.
    assert finish_assessment(sitting, path=db) == 75

    past = history(uid, path=db)
    assert len(past) == 1 and past[0]["overall"] == 75 and past[0]["answered"] == 3, past
    assert past[0]["topic"] == "DSA" and past[0]["questions"] == 3

    full = assessment(uid, sitting, path=db)
    assert len(full["answers"]) == 3
    first = next(a for a in full["answers"] if a["question_id"] == qs[0]["id"])
    assert first["transcript"] == "better answer" and first["score"] == 90
    assert first["report"]["comments"]["strengths"] == ["clear"], "report round-trips as JSON"
    assert next(a for a in full["answers"] if a["score"] is None)["report"] is None
    assert full["answers"][0]["text"], "the question text comes back for the report page"

    # An open sitting is not history yet, and another user cannot read either one.
    open_sitting = start_assessment(uid, None, "beginner", 5, path=db)
    assert len(history(uid, path=db)) == 1, "an unfinished sitting must not appear in history"
    assert assessment(uid, open_sitting, path=db)["topic"] is None  # every topic

    other = create_user("bob@example.com", "Bob", "x" * 12, path=db)
    assert assessment(other, sitting, path=db) is None, "one user must not read another's answers"
    assert not owns_assessment(other, open_sitting, path=db), "nor write to their sitting"
    assert history(other, path=db) == []


def _check_recordings(db, uid):
    """Bytes must come back byte-identical, a re-take must replace, and both must expire."""
    qs = questions(path=db)[:2]
    sitting = start_assessment(uid, None, "advanced", len(qs), path=db)

    audio = bytes(range(256)) * 40  # every byte value, so a text round-trip would corrupt it
    save_recording(sitting, qs[0]["id"], audio, 12.5, path=db)
    stored = recording(sitting, qs[0]["id"], path=db)
    assert stored["audio"] == audio, "a blob must survive the round-trip unchanged"
    assert stored["seconds"] == 12.5 and stored["mime"] == "audio/ogg"
    assert recording(sitting, "no-such-question", path=db) is None

    # A re-take replaces rather than piling up, same as answers.
    save_recording(sitting, qs[0]["id"], b"second take", 3.0, path=db)
    with connect(db) as conn:
        assert conn.execute("SELECT count(*) c FROM recordings").fetchone()["c"] == 1
    assert recording(sitting, qs[0]["id"], path=db)["audio"] == b"second take"

    # A take with no answer row is what a re-run of ASR would pick up; once the answer
    # lands it drops out of the queue.
    save_recording(sitting, qs[1]["id"], b"waiting", 8.0, path=db)
    assert len(untranscribed(path=db)) == 2
    assert "audio" not in untranscribed(path=db)[0], "the queue query must not carry blobs"
    save_answer(sitting, qs[1]["id"], "transcribed already", 8.0, path=db)
    queued = untranscribed(path=db)
    assert [q["question_id"] for q in queued] == [qs[0]["id"]], queued

    assert prune_recordings(path=db) == 0, "a fresh take is inside the retention window"
    with connect(db) as conn:  # backdate past the window rather than waiting a month
        conn.execute("UPDATE recordings SET created_at = datetime('now', '-31 days')")
    assert prune_recordings(path=db) == 2
    assert recording(sitting, qs[0]["id"], path=db) is None

    # Deleting the sitting takes its audio with it — the cascade is why foreign_keys is on.
    save_recording(sitting, qs[0]["id"], b"cascade me", 5.0, path=db)
    with connect(db) as conn:
        conn.execute("DELETE FROM assessments WHERE id = ?", (sitting,))
        assert conn.execute("SELECT count(*) c FROM recordings").fetchone()["c"] == 0, (
            "deleting an assessment must delete its recordings"
        )


def _check_jobs(db, uid):
    """Claiming takes each row once, a stage advance resets attempts, and retries run out."""
    qs = questions(path=db)[:2]
    sitting = start_assessment(uid, None, "expert", len(qs), path=db)
    for q in qs:
        enqueue(sitting, q["id"], path=db)
    assert assessment_jobs_left(sitting, path=db) == 2

    # Every claim must be a different row: this is the whole reason for UPDATE ... RETURNING.
    claimed = [claim_job(path=db), claim_job(path=db)]
    assert len({j["id"] for j in claimed}) == 2, claimed
    assert claim_job(path=db) is None, "nothing left to claim once both are running"
    assert all(j["kind"] == "transcribe" and j["attempts"] == 1 for j in claimed), claimed

    # ASR done: the job moves to the judge with a clean attempt count.
    first, second = claimed
    save_answer(sitting, first["question_id"], "what I said", 30.0, path=db)
    advance_job(first["id"], "grade", path=db)
    again = claim_job(path=db)
    assert again["id"] == first["id"] and again["kind"] == "grade", again
    assert again["attempts"] == 1, "a new stage starts its retries over"

    ready = job_input(sitting, first["question_id"], path=db)
    assert ready["level"] == "expert" and ready["transcript"] == "what I said"
    assert ready["ideal_answer"], "the judge needs the ideal answer"

    verdict = {"score": 72, "parameters": {}, "comments": {}}
    assert grade_answer(sitting, first["question_id"], verdict, path=db) == 1
    finish_job(again["id"], path=db)
    # The average moves as verdicts land, before the sitting is closed.
    assert rescore_assessment(sitting, path=db) == 72
    assert assessment_jobs_left(sitting, path=db) == 1

    # The second one keeps failing: retried until its attempts run out, then parked.
    assert fail_job(second["id"], "asr 502", path=db)["status"] == "pending", "retry first"
    for _ in range(MAX_JOB_ATTEMPTS - 1):
        parked = fail_job(claim_job(path=db)["id"], "asr 502 again", path=db)
    assert parked["status"] == "failed", parked
    assert claim_job(path=db) is None, "a parked job must not be claimed again"
    assert assessment_jobs_left(sitting, path=db) == 0, "failed is finished, just not graded"

    # The failure is visible to the report page, with its reason.
    full = assessment(uid, sitting, path=db)
    assert full["stuck"] == 1 and full["working"] == 0, full
    assert [j["error"] for j in full["jobs"]] == ["asr 502 again"], full["jobs"]
    assert history(uid, path=db)[0]["working"] == 0, "an unfinished sitting is not history yet"

    # A crash leaves a row 'running' with nobody on it; the watchdog is what frees it.
    enqueue(sitting, qs[1]["id"], path=db)  # re-submitting the open sitting retries it
    assert claim_job(path=db)["attempts"] == 1, "a retry starts its attempts over"
    assert requeue_stale_jobs(path=db) == 0, "a fresh claim is not stale"
    with connect(db) as conn:
        conn.execute("UPDATE jobs SET claimed_at = datetime('now', '-9 minutes')")
    assert requeue_stale_jobs(path=db) == 1
    assert claim_job(path=db)["question_id"] == qs[1]["id"], "and it is claimable again"

    with connect(db) as conn:  # the cascade, same as recordings
        conn.execute("DELETE FROM assessments WHERE id = ?", (sitting,))
        assert conn.execute("SELECT count(*) c FROM jobs").fetchone()["c"] == 0


if __name__ == "__main__":
    demo()
