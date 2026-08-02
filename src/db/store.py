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

    print(f"ok — {len(bank)} questions seeded, signup/login round-trips, duplicates rejected")


if __name__ == "__main__":
    demo()
