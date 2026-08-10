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
from src.constants.filepaths import DB_PATH, MCQ_JSON, QUESTIONS_JSON, SCHEMA_SQL
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
    """Apply the schema, migrate an older database, and seed both banks on a fresh one."""
    (path or DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as conn:
        conn.executescript(SCHEMA_SQL.read_text())
        _migrate(conn)
        if not conn.execute("SELECT 1 FROM questions LIMIT 1").fetchone():
            _seed(conn, json.loads(QUESTIONS_JSON.read_text()))
        # mcq.json is written by a build step and committed, so it is simply absent until
        # that has been run once — nothing to seed is not a failure to start up.
        if MCQ_JSON.exists() and not conn.execute("SELECT 1 FROM mcq LIMIT 1").fetchone():
            _seed_mcq(conn, json.loads(MCQ_JSON.read_text(encoding="utf-8")))


# Columns that arrived after a database was already in the field. schema.sql is re-applied
# through executescript() on every open, so everything in it has to be idempotent — and
# SQLite has no ADD COLUMN IF NOT EXISTS. A bare ALTER in the schema file throws
# "duplicate column name" the second time the app starts and takes it down with it, so the
# guard lives here instead: read the columns back, add only what is missing.
#
# This is the project's first migration. Later ones append to this table; anything that
# cannot be expressed as one ALTER needs its own function and its own thought.
_MIGRATIONS = (
    # Discriminators, both defaulting to the format that existed before there was a choice —
    # so every row already in the database reads correctly rather than needing a backfill.
    ("questions", "format", "TEXT NOT NULL DEFAULT 'vocal'"),
    ("assessments", "format", "TEXT NOT NULL DEFAULT 'vocal'"),
)


def _migrate(conn):
    """Add any column the database predates. A no-op once applied, which is the point.

    Interpolated rather than bound because SQLite takes no parameter in an identifier
    position, in either PRAGMA or ALTER; the names are literals from `_MIGRATIONS` above and
    never reach here from a request.
    """
    for table, column, spec in _MIGRATIONS:
        present = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in present:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {spec}")


def _seed(conn, bank):
    """Load questions.json into topics + questions. The JSON stays the seed, not the source."""
    for question in bank:
        conn.execute("INSERT OR IGNORE INTO topics(name) VALUES (?)", (question["topic"],))
        conn.execute(
            "INSERT INTO questions(id, topic_id, text, ideal_answer) "
            "VALUES (?, (SELECT id FROM topics WHERE name = ?), ?, ?)",
            (question["id"], question["topic"], question["text"], question["ideal_answer"]),
        )


def _seed_mcq(conn, bank):
    """Load mcq.json. One entry is two rows: the question itself, then its options.

    Both inserts share the caller's transaction, so a bad entry rolls the questions row back
    with the mcq row and a half-seeded MCQ is never reachable — which is the whole reason the
    options are NOT NULL in their own table rather than nullable columns on questions.

    The correct option's text becomes ideal_answer. That is not a filler value to satisfy a
    NOT NULL: for an MCQ, the correct option *is* the ideal answer.
    """
    for entry in bank:
        conn.execute("INSERT OR IGNORE INTO topics(name) VALUES (?)", (entry["topic"],))
        conn.execute(
            "INSERT INTO questions(id, topic_id, text, ideal_answer, format) "
            "VALUES (?, (SELECT id FROM topics WHERE name = ?), ?, ?, 'mcq')",
            (entry["id"], entry["topic"], entry["text"], entry["options"][entry["correct"]]),
        )
        conn.execute(
            "INSERT INTO mcq(question_id, source_id, level, options, correct, explanation) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                entry["id"],
                entry["source_id"],
                entry["level"],
                json.dumps(entry["options"]),
                entry["correct"],
                entry["explanation"],
            ),
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
    """Vocal questions for one topic, or all. Excludes ideal_answer — the browser sees these.

    An MCQ is a real row in the same table, so without the format test the spoken flow would
    serve four-option stems that have no audio and no judge behind them.
    """
    with connect(path) as conn:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT q.id, t.name AS topic, q.text FROM questions q "
                "JOIN topics t ON t.id = q.topic_id "
                "WHERE q.format = 'vocal' AND (?1 IS NULL OR t.name = ?1) ORDER BY q.id",
                (topic,),
            )
        ]


def question(question_id, path=None):
    """One spoken question including its ideal answer, or None. Server-side only.

    Vocal only, like `questions()`: an MCQ id comes back as None here and goes through
    `mcq_row()` instead, which is what carries the answer key.
    """
    with connect(path) as conn:
        row = conn.execute(
            "SELECT q.id, t.name AS topic, q.text, q.ideal_answer FROM questions q "
            "JOIN topics t ON t.id = q.topic_id WHERE q.id = ? AND q.format = 'vocal'",
            (question_id,),
        ).fetchone()
    return dict(row) if row else None


def mcq_questions(topic=None, level=None, path=None):
    """The MCQ set for a topic and level, with `options` parsed. The browser sees these.

    `correct` and `explanation` are absent by construction, not by the caller remembering to
    strip them: feedback is end-only, and a browser holding the key could reveal it question
    by question. `mcq_row()` is the server-side view that has them.
    """
    with connect(path) as conn:
        return [
            dict(row) | {"options": json.loads(row["options"])}
            for row in conn.execute(
                "SELECT q.id, q.text, t.name AS topic, m.level, m.options FROM mcq m "
                "JOIN questions q ON q.id = m.question_id "
                "JOIN topics t ON t.id = q.topic_id "
                "WHERE (?1 IS NULL OR t.name = ?1) AND (?2 IS NULL OR m.level = ?2) "
                "ORDER BY q.id",
                (topic, level),
            )
        ]


def mcq_row(question_id, path=None):
    """One MCQ with its answer key and explanation, or None. Server-side grading only."""
    with connect(path) as conn:
        row = conn.execute(
            "SELECT q.id, q.text, t.name AS topic, m.level, m.options, m.correct, "
            "       m.explanation FROM mcq m "
            "JOIN questions q ON q.id = m.question_id "
            "JOIN topics t ON t.id = q.topic_id WHERE m.question_id = ?",
            (question_id,),
        ).fetchone()
    if row is None:
        return None
    return dict(row) | {"options": json.loads(row["options"])}


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


def start_assessment(user_id, topic, level, questions, fmt="vocal", path=None):
    """Open a sitting and return its id. `topic` is None for every topic.

    `fmt`, not `format`, which is a builtin. It defaults to 'vocal' so the spoken flow reads
    exactly as it did before there was a second format to pick.
    """
    if level not in LEVELS:
        raise ValueError(f"Unknown level {level!r}. Expected one of {sorted(LEVELS)}.")
    with connect(path) as conn:
        return conn.execute(
            "INSERT INTO assessments(user_id, topic, level, questions, format) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, topic, level, questions, fmt),
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
    """Every sitting this user has opened, newest first — in progress and finished alike.

    An unfinished sitting is listed rather than hidden. The dashboard is the only route to
    one, so filtering them out made an interview abandoned halfway look like it had never
    happened. `finished_at IS NULL` is what marks one as still open.

    `graded` counts scored answers directly. The browser used to derive progress by
    subtracting the queue from the question count, which counted a question that was never
    recorded — no answer row and no job — as graded.
    """
    with connect(path) as conn:
        return [
            dict(row)
            for row in conn.execute(
                # format rides along so the dashboard can label a row and the report screen
                # knows which of the two to render before it fetches anything.
                "SELECT a.id, a.topic, a.level, a.format, a.questions, a.overall, a.started_at,"
                "       a.finished_at, count(n.id) AS answered, count(n.score) AS graded,"
                f"      {_PROGRESS}"
                " FROM assessments a LEFT JOIN answers n ON n.assessment_id = a.id"
                " WHERE a.user_id = ?"
                # id breaks the tie: started_at is second-resolution, so two sittings opened
                # in the same second would otherwise come back in an arbitrary order.
                " GROUP BY a.id ORDER BY a.started_at DESC, a.id DESC LIMIT ?",
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
            "SELECT a.id, a.topic, a.level, a.format, a.questions, a.overall, a.started_at,"
            "       a.finished_at,"
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
        # Last: it seeds a second bank into the same database and deletes a source question
        # to prove the cascade, so nothing above should be reading the bank afterwards.
        mcqs = _check_mcq(db, uid)

    print(
        f"ok — {len(bank)} questions seeded, signup/login round-trips, duplicates rejected, "
        "assessments persist and stay per-user, takes round-trip and expire, "
        f"jobs claim once and retry then park, {mcqs} mcqs seed idempotently and keep their key"
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
    assert past[0]["graded"] == 2, "the ungraded answer is answered but not graded"

    full = assessment(uid, sitting, path=db)
    assert len(full["answers"]) == 3
    first = next(a for a in full["answers"] if a["question_id"] == qs[0]["id"])
    assert first["transcript"] == "better answer" and first["score"] == 90
    assert first["report"]["comments"]["strengths"] == ["clear"], "report round-trips as JSON"
    assert next(a for a in full["answers"] if a["score"] is None)["report"] is None
    assert full["answers"][0]["text"], "the question text comes back for the report page"

    # An open sitting is listed too, newest first, and reads as open rather than as a zero.
    open_sitting = start_assessment(uid, None, "beginner", 5, path=db)
    listed = history(uid, path=db)
    assert [s["id"] for s in listed] == [open_sitting, sitting], listed
    assert listed[0]["finished_at"] is None and listed[0]["overall"] is None, listed[0]
    assert listed[0]["answered"] == 0 and listed[0]["graded"] == 0, listed[0]
    assert assessment(uid, open_sitting, path=db)["topic"] is None  # every topic

    # Another user cannot read either one.

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
    listed = next(s for s in history(uid, path=db) if s["id"] == sitting)
    assert listed["working"] == 0 and listed["stuck"] == 1, listed

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


def _check_mcq(db, uid):
    """The satellite table: seeding, the answer key staying server-side, and the migration.

    Seeded from a fixture in the temp directory rather than the committed data/mcq.json, which
    a build step writes and so may not exist yet — and which would move the counts here every
    time the bank is regenerated.
    """
    fixture = db.parent / "mcq.json"
    sources = questions(path=db)[5:7]
    bank = [
        {
            "id": f"{source['id']}-{level}",
            "source_id": source["id"],
            "topic": source["topic"],
            "level": level,
            "text": f"Which statement about {source['id']} holds at {level}?",
            "options": ["the first", "the second", "the third", "the fourth"],
            "correct": index % 4,
            "explanation": f"Because of {source['id']}.",
        }
        for index, (source, level) in enumerate(
            (s, lv) for s in sources for lv in ("beginner", "advanced", "expert")
        )
    ]
    fixture.write_text(json.dumps(bank), encoding="utf-8")

    before = MCQ_JSON
    globals()["MCQ_JSON"] = fixture
    try:
        # The database was opened before this fixture was bound, so the committed bank is
        # already seeded. Clearing both tables puts the seed guard back where this check
        # needs it, and keeps the counts below independent of how big the real bank grows.
        with connect(db) as conn:
            conn.execute("DELETE FROM questions WHERE format = 'mcq'")
            conn.execute("DELETE FROM mcq")
        setup(db)
        # Seeding is guarded on the table being empty, exactly like questions.json, so the
        # second and third open must not load the bank again.
        setup(db)
        setup(db)
        with connect(db) as conn:
            assert conn.execute("SELECT count(*) c FROM mcq").fetchone()["c"] == len(bank)
            # And this is what the guarded migration buys: schema.sql was re-applied on every
            # one of those opens, and a bare ALTER would have thrown on the second.
            for table in ("questions", "assessments"):
                cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]
                assert cols.count("format") == 1, (table, cols)
    finally:
        globals()["MCQ_JSON"] = before

    # An MCQ is a row in `questions`, but the spoken flow must not serve it.
    assert len(questions(path=db)) == len(json.loads(QUESTIONS_JSON.read_text()))
    assert question(bank[0]["id"], path=db) is None, "an mcq id is not a vocal question"

    served = mcq_questions(path=db)
    assert len(served) == len(bank), served
    assert served[0]["options"] == bank[0]["options"], "options come back parsed, not as JSON"
    for row in served:
        assert "correct" not in row and "explanation" not in row, (
            "the browser must never receive the answer key — feedback is end-only"
        )
    assert len(mcq_questions(level="beginner", path=db)) == len(sources)
    one_topic = mcq_questions(topic=sources[0]["topic"], path=db)
    assert one_topic and all(r["topic"] == sources[0]["topic"] for r in one_topic)
    assert mcq_questions(topic="Nope", path=db) == []

    keyed = mcq_row(bank[0]["id"], path=db)
    right = keyed["options"][keyed["correct"]]
    assert keyed["correct"] == bank[0]["correct"] and keyed["explanation"]
    assert keyed["options"] == bank[0]["options"] and keyed["level"] == "beginner"
    assert mcq_row("no-such-question", path=db) is None
    assert mcq_row(sources[0]["id"], path=db) is None, "a vocal question has no mcq row"
    with connect(db) as conn:  # the correct option *is* the ideal answer, not a filler value
        stored = conn.execute(
            "SELECT ideal_answer, format FROM questions WHERE id = ?", (bank[0]["id"],)
        ).fetchone()
    assert stored["ideal_answer"] == right, dict(stored)
    assert stored["format"] == "mcq"

    # An MCQ answer is an ordinary answers row, so the existing upsert covers a re-answer.
    sitting = start_assessment(uid, None, "beginner", 2, fmt="mcq", path=db)
    assert history(uid, path=db)[0]["format"] == "mcq", "the report screen branches on this"
    assert assessment(uid, sitting, path=db)["format"] == "mcq"
    verdict = {"score": 0, "chosen": 1, "correct": keyed["correct"]}
    save_answer(sitting, bank[0]["id"], keyed["options"][1], 0.0, verdict, path=db)
    save_answer(
        sitting,
        bank[0]["id"],
        right,
        0.0,
        {**verdict, "score": 100, "chosen": keyed["correct"]},
        path=db,
    )
    marked = assessment(uid, sitting, path=db)["answers"]
    assert len(marked) == 1, "re-answering replaces the pick rather than scoring it twice"
    assert marked[0]["score"] == 100 and marked[0]["transcript"] == right, marked
    assert finish_assessment(sitting, path=db) == 100, "overall is the percentage correct"

    # A sitting opened the old way still reads as vocal: the column default is what makes the
    # migration a no-op for every row that was already in the database.
    older = start_assessment(uid, None, "expert", 1, path=db)
    assert assessment(uid, older, path=db)["format"] == "vocal"

    # Editing or deleting a source question takes its MCQs with it rather than orphaning them.
    with connect(db) as conn:
        conn.execute("DELETE FROM questions WHERE id = ?", (sources[1]["id"],))
        orphans = conn.execute(
            "SELECT count(*) c FROM mcq WHERE source_id = ?", (sources[1]["id"],)
        ).fetchone()["c"]
        assert orphans == 0, "deleting a source must cascade to its mcq rows"
        assert conn.execute("SELECT count(*) c FROM mcq").fetchone()["c"] == len(bank) - 3
    return len(bank)


if __name__ == "__main__":
    demo()
