"""Mock interview practice API. Speech runs on the HF Inference API; see evaluate for the judge."""

import contextlib
import io
import logging
import sqlite3
import time

import numpy as np
import soundfile as sf
from fastapi import (
    Cookie,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Response,
    UploadFile,
)
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse

from app import setup_logging
from app.evaluate import evaluate, grade_choice
from app.worker import start as start_worker
from app.worker import stop as stop_worker
from src.constants.constants import (
    COOKIE_MAX_AGE,
    COOKIE_NAME,
    LEVELS,
    MAX_UPLOAD_BYTES,
    MIN_PASSWORD,
    MIN_RMS,
    MIN_SECONDS,
    TTS_VOICE_DEFAULT,
)
from src.constants.filepaths import AUDIO_DIR, INDEX_HTML, spoken
from src.db import store
from src.helpers.inference import transcribe
from src.helpers.security import sign, unsign

setup_logging()  # also covers `uvicorn app.main:app`, which never calls run()
log = logging.getLogger("app.api")

store.setup()  # creates and seeds data/practice.db on first run


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    """Run the grader for as long as the server runs, and not at import time.

    At import this module is also loaded by the test suite, and a thread draining a
    half-built temp database is nobody's idea of a fixture.
    """
    start_worker()
    yield
    stop_worker()


app = FastAPI(title="Placemint.Academy", lifespan=lifespan)

# The two interview formats that grade. Telephonic and video have cards on the front page but
# are disabled, and no route accepts them, so they are deliberately not listed here.
FORMATS = ("vocal", "mcq")


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
def questions(
    topic: str | None = None,
    level: str | None = None,
    fmt: str = Query("vocal", alias="format"),
    user: dict = Depends(current_user),
):
    """The bank for one topic — spoken by default, four-option stems with `?format=mcq`.

    One route rather than a sibling: both formats are the same table filtered differently and
    the browser asks for a set by topic either way, so a second route would repeat the auth
    dependency, the empty-set 404 and the topic message to change one call in the store.

    `level` narrows only the MCQ bank, and that asymmetry is the format's whole design: for a
    right-or-wrong answer the level picks which questions you get, where for a spoken one it
    shifts the judge instead. `fmt`, not `format`, because the builtin is worth not shadowing;
    the alias keeps the url the design named.
    """
    if fmt not in FORMATS:
        raise HTTPException(422, f"Unknown format {fmt!r}. Expected one of {sorted(FORMATS)}.")
    if level is not None and level not in LEVELS:
        raise HTTPException(422, f"Unknown level {level!r}. Expected one of {sorted(LEVELS)}.")

    picked = store.mcq_questions(topic, level) if fmt == "mcq" else store.questions(topic)
    if not picked:
        raise HTTPException(404, f"No {fmt} questions for topic {topic!r}. Have: {store.topics()}")
    return picked


@app.get("/levels")
def levels():
    return list(LEVELS)


@app.get("/questions/{question_id}/audio")
def question_audio(question_id: str, user: dict = Depends(current_user)):
    """The question spoken aloud, if `python -m src.helpers.speech` has generated it.

    404 when it has not, which is a normal state rather than a fault: the file is a build
    artifact, and the interview reads the question as text either way. Nothing synthesises
    on demand here — that would put a ~70s remote call in a GET.
    """
    take = spoken(question_id, TTS_VOICE_DEFAULT)
    # resolve() before the check: a question_id of '../../etc/passwd' would otherwise walk
    # out of the directory. Ids come from the database, but this one arrives in the url.
    if not take.resolve().is_relative_to(AUDIO_DIR.resolve()) or not take.is_file():
        raise HTTPException(404, "That question has not been recorded yet.")
    return FileResponse(take, media_type="audio/ogg")


@app.post("/assessments", status_code=201)
def open_assessment(
    level: str = Form(...),
    questions: int = Form(...),
    topic: str | None = Form(None),
    fmt: str = Form("vocal", alias="format"),
    user: dict = Depends(current_user),
):
    """Open a sitting. The browser calls this when the candidate starts an interview.

    `format` is stored rather than derived: the report screen has to know which of the two to
    render before it has fetched a single answer.
    """
    if level not in LEVELS:
        raise HTTPException(422, f"Unknown level {level!r}. Expected one of {sorted(LEVELS)}.")
    if fmt not in FORMATS:
        raise HTTPException(422, f"Unknown format {fmt!r}. Expected one of {sorted(FORMATS)}.")
    # An empty topic field means every topic, which is stored as NULL.
    topic = topic or None
    if topic is not None and topic not in store.topics():
        raise HTTPException(404, f"Unknown topic {topic!r}")

    assessment_id = store.start_assessment(user["id"], topic, level, questions, fmt)
    log.info(
        "assessment %d opened user=%d format=%s topic=%s level=%s n=%d",
        assessment_id,
        user["id"],
        fmt,
        topic or "*",
        level,
        questions,
    )
    return {"id": assessment_id}


@app.post("/assessments/{assessment_id}/mcq", status_code=201)
def answer_mcq(
    assessment_id: int,
    question_id: str = Form(...),
    choice: int = Form(...),
    user: dict = Depends(current_user),
):
    """Mark one multiple-choice pick. Nothing remote runs, so 201 rather than /submit's 202.

    The reply is `{"recorded": true}` and deliberately not the verdict: a browser that could see
    correctness here could walk the answer key question by question, which is the property the
    format rests on. The pick is graded and stored — the report is where it becomes visible.
    """
    if not store.owns_assessment(user["id"], assessment_id):
        log.warning(
            "mcq rejected: user %d cannot write to assessment %d", user["id"], assessment_id
        )
        raise HTTPException(404, "No such open assessment.")
    question = store.mcq_row(question_id)
    if question is None:
        # Also the answer for a spoken question posted here: it has no options and no key.
        log.warning("mcq rejected: unknown question_id %r", question_id)
        raise HTTPException(404, f"Unknown question_id {question_id!r}")
    try:
        report = grade_choice(question, choice)
    except ValueError as e:
        # The one place a route converts a domain error into a status: evaluate.py raises
        # ValueError because it does not know HTTP exists, and that is its whole contract.
        raise HTTPException(400, str(e)) from e

    # seconds=0.0 because there is no recording, and so no recordings row and no jobs row —
    # the worker never sees an MCQ sitting. rescore rather than waiting for finish, so overall
    # climbs as picks land exactly as it does on the spoken path.
    store.save_answer(assessment_id, question_id, report["transcript"], 0.0, report)
    store.rescore_assessment(assessment_id)
    log.info(
        "mcq %s marked for assessment %d score=%d", question_id, assessment_id, report["score"]
    )
    return {"recorded": True}


@app.post("/assessments/{assessment_id}/finish")
def close_assessment(assessment_id: int, user: dict = Depends(current_user)):
    """Close a sitting to further answers. Grading carries on in the worker after this.

    Idempotent, and it does not wait: `overall` is whatever has been graded so far and the
    worker raises it as verdicts land. `working` is what the browser polls on.
    """
    if store.assessment(user["id"], assessment_id) is None:
        raise HTTPException(404, "No such assessment.")
    overall = store.finish_assessment(assessment_id)
    left = store.assessment_jobs_left(assessment_id)
    log.info(
        "assessment %d finished user=%d overall=%s awaiting=%d",
        assessment_id,
        user["id"],
        overall,
        left,
    )
    return {"id": assessment_id, "overall": overall, "working": left}


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


@app.post("/submit", status_code=202)
async def submit(
    question_id: str = Form(...),
    audio: UploadFile = File(...),
    level: str = Form("advanced"),
    assessment_id: int | None = Form(None),
    user: dict = Depends(current_user),
):
    """Store the take and queue it. Grading happens in the worker; nobody waits here.

    202, not 200: the answer is accepted, not graded. A submit without an assessment_id has
    nothing to queue against, so it grades inline — `curl -F question_id=... /submit` stays
    a one-command way to exercise the whole chain.
    """
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

    # Before the gate and before anything can fail: the take is the evidence.
    await run_in_threadpool(_keep, assessment_id, question_id, mono, rate, seconds)

    # Whisper hallucinates fluent text ("Thank you very much...") from silence, so never
    # send it a dead recording — and never spend a job on one either. The zero verdict is
    # local and instant, so it is written here rather than queued.
    # ponytail: flat RMS gate, no VAD.
    if seconds < MIN_SECONDS or float(np.sqrt(np.mean(mono**2))) < MIN_RMS:
        log.info("submit %s: silence gate tripped, skipping ASR", question_id)
        report = evaluate("", question["text"], question["ideal_answer"], level)
        _persist(assessment_id, question_id, "", seconds, report)
        return {"question_id": question_id, "queued": False, "report": report}

    if assessment_id is None:
        # No sitting to queue against, so grade inline: `curl -F question_id=... /submit`
        # stays a one-command check of the whole chain, and there is nobody to poll.
        return {
            "question_id": question_id,
            "queued": False,
            "report": await run_in_threadpool(_grade_now, question, level, raw),
        }

    store.enqueue(assessment_id, question_id)
    log.info("submit %s queued for assessment %d", question_id, assessment_id)
    return {"question_id": question_id, "queued": True, "report": None}


def _grade_now(question, level, raw):
    """The old synchronous path, kept for a submit with no sitting behind it.

    Runs in the threadpool because it blocks for ~25s on two remote calls. Nothing polls
    for it, so a failure has to become a status rather than a queued retry.
    """
    try:
        started = time.perf_counter()
        transcript = transcribe("asr", raw)
        log.info(
            "asr ok in %.1fs — %d words", time.perf_counter() - started, len(transcript.split())
        )
        return evaluate(transcript, question["text"], question["ideal_answer"], level)
    except Exception as e:
        log.exception("inline grading failed for %s", question["id"])
        raise HTTPException(502, f"Could not grade that answer: {e}") from e


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
