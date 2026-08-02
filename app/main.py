"""Mock interview practice API. Speech runs on the HF Inference API; see evaluate for the judge."""

import io
import logging
import sqlite3
import time

import numpy as np
import soundfile as sf
from fastapi import Cookie, Depends, FastAPI, File, Form, HTTPException, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse

from app import setup_logging
from app.evaluate import _client, evaluate
from src.constants.constants import (
    ASR_MODEL,
    ASR_OPTIONS,
    COOKIE_MAX_AGE,
    COOKIE_NAME,
    LEVELS,
    MAX_UPLOAD_BYTES,
    MIN_PASSWORD,
    MIN_RMS,
    MIN_SECONDS,
    RECORDING_RETENTION_DAYS,
)
from src.constants.filepaths import INDEX_HTML
from src.db import store
from src.helpers.security import sign, unsign

setup_logging()  # also covers `uvicorn app.main:app`, which never calls run()
log = logging.getLogger("app.api")

store.setup()  # creates and seeds data/practice.db on first run

# Stored audio expires; nothing else here does. Swept at startup because there is no
# worker to hang it off yet — a long-running server keeps whatever it captured until the
# next restart. ponytail: sweep on boot only, move to slice 4's worker poll.
if _expired := store.prune_recordings():
    log.info("pruned %d recording(s) past the %d-day window", _expired, RECORDING_RETENTION_DAYS)

app = FastAPI(title="Mock Interview Practice")


def current_user(session: str | None = Cookie(None, alias=COOKIE_NAME)) -> dict:
    """The signed-in user, or 401. Every route below the login screen depends on this."""
    user_id = unsign(session) if session else None
    profile = store.profile(user_id) if user_id else None
    if profile is None:
        raise HTTPException(401, "Sign in to continue.")
    return profile


def _start_session(response: Response, user_id: int) -> None:
    # HttpOnly keeps the cookie away from any injected script; SameSite=Lax blocks
    # cross-site use. secure=True would break http://localhost, so it is left off
    # here. ponytail: no Secure flag, set it from an env switch when this gets a domain.
    response.set_cookie(
        COOKIE_NAME, sign(user_id), max_age=COOKIE_MAX_AGE, httponly=True, samesite="lax"
    )


@app.get("/")
def index():
    return FileResponse(INDEX_HTML)


@app.post("/signup", status_code=201)
def signup(
    response: Response,
    email: str = Form(...),
    name: str = Form(...),
    password: str = Form(...),
    level: str = Form("advanced"),
):
    """Open signup: anyone can create an account, and it signs them straight in."""
    if "@" not in email or email.strip() != email.strip().replace(" ", ""):
        raise HTTPException(422, "Enter a valid email address.")
    if not name.strip():
        raise HTTPException(422, "Enter your name.")
    if len(password) < MIN_PASSWORD:
        raise HTTPException(422, f"Use at least {MIN_PASSWORD} characters for your password.")
    if level not in LEVELS:
        raise HTTPException(422, f"Unknown level {level!r}. Expected one of {sorted(LEVELS)}.")

    user_id = store.create_user(email, name, password, level)
    if user_id is None:
        raise HTTPException(409, "That email already has an account. Sign in instead.")
    _start_session(response, user_id)
    log.info("signup ok user=%d", user_id)
    return store.profile(user_id)


@app.post("/login")
def login(response: Response, email: str = Form(...), password: str = Form(...)):
    user_id = store.authenticate(email, password)
    if user_id is None:
        # One message for both causes: a distinct "no such account" reply would let
        # anyone enumerate which addresses are registered.
        log.info("login rejected")
        raise HTTPException(401, "That email and password don't match an account.")
    _start_session(response, user_id)
    log.info("login ok user=%d", user_id)
    return store.profile(user_id)


@app.post("/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE_NAME, httponly=True, samesite="lax")
    return {"ok": True}


@app.get("/me")
def me(user: dict = Depends(current_user)):
    """Who the cookie says you are. The frontend calls this on load to skip the login screen."""
    return user


@app.get("/topics")
def topics(user: dict = Depends(current_user)):
    return store.topics()


@app.get("/questions")
def questions(topic: str | None = None, user: dict = Depends(current_user)):
    picked = store.questions(topic)
    if not picked:
        raise HTTPException(404, f"Unknown topic {topic!r}. Available: {store.topics()}")
    return picked


@app.get("/levels")
def levels():
    return list(LEVELS)


@app.post("/assessments", status_code=201)
def open_assessment(
    level: str = Form(...),
    questions: int = Form(...),
    topic: str | None = Form(None),
    user: dict = Depends(current_user),
):
    """Open a sitting. The browser calls this when the candidate starts an interview."""
    if level not in LEVELS:
        raise HTTPException(422, f"Unknown level {level!r}. Expected one of {sorted(LEVELS)}.")
    # An empty topic field means every topic, which is stored as NULL.
    topic = topic or None
    if topic is not None and topic not in store.topics():
        raise HTTPException(404, f"Unknown topic {topic!r}")

    assessment_id = store.start_assessment(user["id"], topic, level, questions)
    log.info(
        "assessment %d opened user=%d topic=%s level=%s n=%d",
        assessment_id,
        user["id"],
        topic or "*",
        level,
        questions,
    )
    return {"id": assessment_id}


@app.post("/assessments/{assessment_id}/finish")
def close_assessment(assessment_id: int, user: dict = Depends(current_user)):
    """Close a sitting and return its average. Idempotent — a re-finish returns the same."""
    if store.assessment(user["id"], assessment_id) is None:
        raise HTTPException(404, "No such assessment.")
    overall = store.finish_assessment(assessment_id)
    log.info("assessment %d finished user=%d overall=%s", assessment_id, user["id"], overall)
    return {"id": assessment_id, "overall": overall}


@app.get("/history")
def past_assessments(user: dict = Depends(current_user)):
    return store.history(user["id"])


@app.get("/assessments/{assessment_id}")
def one_assessment(assessment_id: int, user: dict = Depends(current_user)):
    """A past sitting with every answer. Scoped to the caller — 404 for anyone else's."""
    past = store.assessment(user["id"], assessment_id)
    if past is None:
        raise HTTPException(404, "No such assessment.")
    return past


@app.post("/submit")
async def submit(
    question_id: str = Form(...),
    audio: UploadFile = File(...),
    level: str = Form("advanced"),
    assessment_id: int | None = Form(None),
    user: dict = Depends(current_user),
):
    question = store.question(question_id)
    if question is None:
        log.warning("submit rejected: unknown question_id %r", question_id)
        raise HTTPException(404, f"Unknown question_id {question_id!r}")
    if level not in LEVELS:
        log.warning("submit rejected: unknown level %r", level)
        raise HTTPException(422, f"Unknown level {level!r}. Expected one of {sorted(LEVELS)}.")
    # Checked before the expensive work: an answer filed against someone else's sitting,
    # or a closed one, must fail before it costs an ASR call.
    if assessment_id is not None and not store.owns_assessment(user["id"], assessment_id):
        log.warning(
            "submit rejected: user %d cannot write to assessment %s", user["id"], assessment_id
        )
        raise HTTPException(404, "No such open assessment.")

    raw = await audio.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        log.warning("submit rejected: %d bytes exceeds the cap", len(raw))
        raise HTTPException(413, f"That recording is over {MAX_UPLOAD_BYTES // 1024 // 1024} MB.")
    try:
        # The browser sends 16 kHz mono WAV; always_2d keeps the mean() below valid.
        samples, rate = sf.read(io.BytesIO(raw), dtype="float32", always_2d=True)
    except Exception as e:
        log.warning("submit rejected: undecodable audio (%d bytes): %s", len(raw), e)
        raise HTTPException(400, f"Could not decode audio: {e}") from e

    mono = samples.mean(axis=1)
    seconds = len(mono) / rate
    log.info("submit %s level=%s audio=%.1fs %dkB", question_id, level, seconds, len(raw) // 1024)

    # Before the gate and before ASR: whatever happens next, the take is the evidence.
    await run_in_threadpool(_keep, assessment_id, question_id, mono, rate, seconds)

    # Whisper hallucinates fluent text ("Thank you very much...") from silence, so
    # never send it a dead recording. ponytail: flat RMS gate, no VAD.
    if seconds < MIN_SECONDS or float(np.sqrt(np.mean(mono**2))) < MIN_RMS:
        log.info("submit %s: silence gate tripped, skipping ASR", question_id)
        report = evaluate("", question["text"], question["ideal_answer"], level)
        _persist(assessment_id, question_id, "", seconds, report)
        return report

    try:
        started = time.perf_counter()
        result = _client().automatic_speech_recognition(
            raw, model=ASR_MODEL, extra_body=ASR_OPTIONS
        )
        transcript = result.text.strip()
        log.info(
            "asr ok in %.1fs — %d words", time.perf_counter() - started, len(transcript.split())
        )
    except Exception as e:
        log.exception("asr failed for %s", question_id)
        raise HTTPException(502, f"Speech-to-text failed: {e}") from e

    try:
        started = time.perf_counter()
        report = evaluate(transcript, question["text"], question["ideal_answer"], level)
    except Exception as e:
        # Save the transcript even though grading failed. It is the expensive half — ASR
        # already ran — and slice 4's worker retries from stored text, not from audio.
        log.exception("grading failed for %s", question_id)
        _persist(assessment_id, question_id, transcript, seconds, None)
        raise HTTPException(502, f"Scoring failed: {e}") from e

    log.info(
        "graded %s level=%s in %.1fs — total=%d %s",
        question_id,
        level,
        time.perf_counter() - started,
        report["score"],
        {p: v["score"] for p, v in report["parameters"].items()},
    )
    _persist(assessment_id, question_id, transcript, seconds, report)
    return report


def _keep(assessment_id, question_id, mono, rate, seconds):
    """Store the take as Opus, so ASR can be re-run and a score can be disputed.

    Opus rather than the WAV the browser sent: 9x smaller for the same 16 kHz speech, which
    is what makes the retention window affordable. libsndfile encodes it in-process, so
    this still needs no ffmpeg. Runs in the threadpool — it is ~1s of CPU per minute of
    audio and would otherwise block the event loop.

    Failing to store the take must not cost the candidate their report, so a storage or
    encoder failure is logged and swallowed.
    """
    if assessment_id is None:
        return  # a one-off curl submit: nothing to attach the take to
    try:
        buf = io.BytesIO()
        sf.write(buf, mono, rate, format="OGG", subtype="OPUS")
        opus = buf.getvalue()
        store.save_recording(assessment_id, question_id, opus, seconds)
        log.info("kept %s: %dkB opus from %.1fs", question_id, len(opus) // 1024, seconds)
    except sqlite3.Error, sf.LibsndfileError, RuntimeError:
        log.exception("could not store the take for %s", question_id)


def _persist(assessment_id, question_id, transcript, seconds, report):
    """Store one answer, if this submission belongs to a sitting.

    assessment_id is optional so `curl -F question_id=... /submit` still works for a
    one-off check. A storage failure must not lose a report the candidate is waiting on,
    so it is logged and swallowed rather than turned into a 502.
    """
    if assessment_id is None:
        return
    try:
        store.save_answer(assessment_id, question_id, transcript, seconds, report)
    except sqlite3.Error:
        log.exception("could not save answer for %s in assessment %s", question_id, assessment_id)
