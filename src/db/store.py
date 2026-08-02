"""Every SQL statement in the project. Nothing here knows HTTP exists.

Connections are opened per call rather than pooled: SQLite opens in microseconds, and
one connection per call means no thread-affinity rules for FastAPI's threadpool to break.
ponytail: connection per call, add a per-thread cache if profiling ever shows it matters.
"""

import contextlib
import json
import sqlite3

from src.constants.constants import LEVELS
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


def history(user_id, limit=20, path=None):
    """A user's finished sittings, newest first, for the history page."""
    with connect(path) as conn:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT a.id, a.topic, a.level, a.questions, a.overall, a.started_at,"
                "       a.finished_at, count(n.id) AS answered"
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
            "SELECT id, topic, level, questions, overall, started_at, finished_at"
            " FROM assessments WHERE id = ? AND user_id = ?",
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

    return dict(head) | {
        "answers": [
            dict(row) | {"report": json.loads(row["report"]) if row["report"] else None}
            for row in answers
        ]
    }


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

    print(
        f"ok — {len(bank)} questions seeded, signup/login round-trips, duplicates rejected, "
        "assessments persist and stay per-user"
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


if __name__ == "__main__":
    demo()
