"""Every route, through a real client, against a throwaway database.

Answers what the self-checks can't: that the cookie actually gates the API, that the
question bank is served from SQLite, and that the silence gate still short-circuits.
No network — the only route that would call out is exercised with silence.
"""

import io
import os
import pathlib
import tempfile
import wave

SILENCE_SECONDS = 2
GATED = ("/me", "/topics", "/questions", "/history", "/assessments/1")


def _silent_wav(seconds=SILENCE_SECONDS, rate=16000):
    """16 kHz mono WAV of pure silence — decodable, but below the RMS gate."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(b"\x00\x00" * rate * seconds)
    return buf.getvalue()


def _client(db):
    """A TestClient whose app talks to `db`.

    store.DB_PATH is rebound before app.main is imported, because importing it runs
    store.setup() at module scope and would otherwise seed the developer's real database.
    """
    os.environ.setdefault("SECRET_KEY", "test-only-key")
    os.environ.setdefault("HF_TOKEN", "unused — no test reaches the network")

    from src.db import store

    store.DB_PATH = db

    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


def _check_locked_out(client):
    for url in GATED:
        assert client.get(url).status_code == 401, f"{url} must require a session"
    assert (
        client.post(
            "/submit",
            data={"question_id": "dsa-1"},
            files={"audio": ("a.wav", _silent_wav(), "audio/wav")},
        ).status_code
        == 401
    ), "/submit must require a session"
    # These two stay open: the login page has to load, and the signup form needs the levels.
    assert client.get("/").status_code == 200
    assert client.get("/levels").status_code == 200


def _check_signup(client):
    rejected = [
        ({"email": "not-an-email", "name": "A", "password": "x" * 12}, 422),
        ({"email": "a@b.com", "name": "   ", "password": "x" * 12}, 422),
        ({"email": "a@b.com", "name": "A", "password": "short"}, 422),
        ({"email": "a@b.com", "name": "A", "password": "x" * 12, "level": "guru"}, 422),
    ]
    for data, want in rejected:
        got = client.post("/signup", data=data).status_code
        assert got == want, f"{data} → {got}, wanted {want}"

    fields = {
        "email": "Ada@Example.com",
        "name": "Ada",
        "password": "correct horse",
        "level": "expert",
    }
    res = client.post("/signup", data=fields)
    assert res.status_code == 201, res.text
    assert res.json() == {
        "id": 1,
        "email": "ada@example.com",  # normalised on the way in
        "name": "Ada",
        "default_level": "expert",
    }, res.json()
    assert client.cookies.get("booth_session"), "signup must sign the new account straight in"
    assert client.post("/signup", data=fields).status_code == 409, "duplicate email must conflict"


def _check_bank(client):
    topics = client.get("/topics").json()
    assert topics == sorted(topics) and len(topics) == 6, topics

    every = client.get("/questions").json()
    assert len(every) == 21, len(every)
    assert "ideal_answer" not in every[0], "the browser must never receive ideal answers"

    dsa = client.get("/questions?topic=DSA").json()
    assert dsa and len(dsa) < len(every) and all(q["topic"] == "DSA" for q in dsa)
    assert client.get("/questions?topic=Nope").status_code == 404


def _check_login(client):
    assert client.post("/logout").status_code == 200
    assert client.get("/me").status_code == 401, "logout must clear the session"

    for bad in ({"password": "wrong"}, {"email": "nobody@example.com"}):
        data = {"email": "ada@example.com", "password": "correct horse"} | bad
        assert client.post("/login", data=data).status_code == 401, data

    # Case and surrounding whitespace must not stop someone signing in.
    assert client.post("/login", data={"email": " ADA@example.com ", "password": "correct horse"})
    assert client.get("/me").json()["name"] == "Ada"

    # A forged cookie must open nothing, however far in the future it claims to expire.
    real = client.cookies.get("booth_session")
    client.cookies.set("booth_session", "1.9999999999.deadbeef")
    assert client.get("/me").status_code == 401, "an unsigned cookie must be refused"
    client.cookies.set("booth_session", real)


def _check_submit(client):
    silence = ("a.wav", _silent_wav(), "audio/wav")
    res = client.post(
        "/submit", data={"question_id": "dsa-1", "level": "beginner"}, files={"audio": silence}
    )
    assert res.status_code == 200, res.text
    assert res.json()["score"] == 0, "silence must score zero without reaching ASR"

    assert (
        client.post("/submit", data={"question_id": "nope"}, files={"audio": silence}).status_code
        == 404
    )
    assert (
        client.post(
            "/submit", data={"question_id": "dsa-1", "level": "guru"}, files={"audio": silence}
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/submit", data={"question_id": "dsa-1"}, files={"audio": ("a.wav", b"not audio")}
        ).status_code
        == 400
    )


def _check_assessments(client):
    """A sitting from open to finish, then the two ways it must not leak."""
    silence = ("a.wav", _silent_wav(), "audio/wav")
    assert client.get("/history").json() == [], "a new account has no history"

    opened = {"level": "beginner", "questions": 2, "topic": "DSA"}
    assert client.post("/assessments", data=opened | {"level": "guru"}).status_code == 422
    assert client.post("/assessments", data=opened | {"topic": "Nope"}).status_code == 404
    res = client.post("/assessments", data=opened)
    assert res.status_code == 201, res.text
    sitting = res.json()["id"]

    for question_id in ("dsa-1", "dsa-2"):
        body = {"question_id": question_id, "level": "beginner", "assessment_id": sitting}
        assert client.post("/submit", data=body, files={"audio": silence}).status_code == 200

    # An open sitting is not history yet, but its answers are already readable.
    assert client.get("/history").json() == []
    live = client.get(f"/assessments/{sitting}").json()
    assert len(live["answers"]) == 2 and live["overall"] is None, live
    assert live["answers"][0]["text"], "the question text comes back for the history page"

    assert client.post(f"/assessments/{sitting}/finish").json() == {"id": sitting, "overall": 0}
    past = client.get("/history").json()
    assert len(past) == 1 and past[0]["answered"] == 2 and past[0]["overall"] == 0, past
    assert past[0]["finished_at"], "a finished sitting must carry its timestamp"

    # A closed sitting takes no more answers, and it must fail before the ASR call.
    closed = {"question_id": "dsa-1", "level": "beginner", "assessment_id": sitting}
    assert client.post("/submit", data=closed, files={"audio": silence}).status_code == 404
    assert client.post("/assessments/9999/finish").status_code == 404
    assert client.get("/assessments/9999").status_code == 404

    return sitting


def _check_isolation(client, sitting):
    """A second account must not read or write the first one's sitting."""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as other:
        signed = other.post(
            "/signup", data={"email": "bob@example.com", "name": "Bob", "password": "x" * 12}
        )
        assert signed.status_code == 201, signed.text
        assert other.get("/history").json() == [], "history is per account"
        assert other.get(f"/assessments/{sitting}").status_code == 404, "not Bob's to read"
        assert other.post(f"/assessments/{sitting}/finish").status_code == 404
        filed = other.post(
            "/submit",
            data={"question_id": "dsa-1", "assessment_id": sitting},
            files={"audio": ("a.wav", _silent_wav(), "audio/wav")},
        )
        assert filed.status_code == 404, "an answer must not land in another account's sitting"


def check():
    """Run the whole API pass. Order matters: the session built by signup carries forward."""
    with tempfile.TemporaryDirectory() as tmp:
        client = _client(pathlib.Path(tmp) / "test.db")
        _check_locked_out(client)
        _check_signup(client)
        _check_bank(client)
        _check_login(client)
        _check_submit(client)
        _check_isolation(client, _check_assessments(client))
    print("ok — routes gated by cookie, signup/login round-trip, bank and sittings from sqlite")


if __name__ == "__main__":
    check()
