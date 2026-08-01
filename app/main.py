"""Mock interview practice API. Speech and scoring both run on the HF Inference API."""

import io
import json
import logging
import pathlib
import time

import numpy as np
import soundfile as sf
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from app import setup_logging
from app.evaluate import LEVELS, _client, evaluate

setup_logging()  # also covers `uvicorn app.main:app`, which never calls run()
log = logging.getLogger("app.api")

HERE = pathlib.Path(__file__).parent
QUESTIONS = {q["id"]: q for q in json.loads((HERE / "questions.json").read_text())}
TOPICS = sorted({q["topic"] for q in QUESTIONS.values()})

# whisper-large-v3 handles Indian English well; pin the language so accented
# input is never mistaken for another one and silently translated.
ASR_MODEL = "openai/whisper-large-v3"
ASR_OPTIONS = {"language": "en", "task": "transcribe"}

app = FastAPI(title="Mock Interview Practice")


@app.get("/")
def index():
    return FileResponse(HERE / "index.html")


@app.get("/topics")
def topics():
    return TOPICS


@app.get("/questions")
def questions(topic: str | None = None):
    picked = [q for q in QUESTIONS.values() if topic in (None, q["topic"])]
    if not picked:
        raise HTTPException(404, f"Unknown topic {topic!r}. Available: {TOPICS}")
    return [{"id": q["id"], "topic": q["topic"], "text": q["text"]} for q in picked]


@app.get("/levels")
def levels():
    return list(LEVELS)


@app.post("/submit")
async def submit(
    question_id: str = Form(...),
    audio: UploadFile = File(...),
    level: str = Form("advanced"),
):
    question = QUESTIONS.get(question_id)
    if question is None:
        log.warning("submit rejected: unknown question_id %r", question_id)
        raise HTTPException(404, f"Unknown question_id {question_id!r}")
    if level not in LEVELS:
        log.warning("submit rejected: unknown level %r", level)
        raise HTTPException(422, f"Unknown level {level!r}. Expected one of {sorted(LEVELS)}.")

    raw = await audio.read()
    try:
        # The browser sends 16 kHz mono WAV; always_2d keeps the mean() below valid.
        samples, rate = sf.read(io.BytesIO(raw), dtype="float32", always_2d=True)
    except Exception as e:
        log.warning("submit rejected: undecodable audio (%d bytes): %s", len(raw), e)
        raise HTTPException(400, f"Could not decode audio: {e}") from e

    mono = samples.mean(axis=1)
    seconds = len(mono) / rate
    log.info("submit %s level=%s audio=%.1fs %dkB", question_id, level, seconds, len(raw) // 1024)

    # Whisper hallucinates fluent text ("Thank you very much...") from silence, so
    # never send it a dead recording. ponytail: flat RMS gate, no VAD.
    if len(mono) < rate or float(np.sqrt(np.mean(mono**2))) < 0.005:
        log.info("submit %s: silence gate tripped, skipping ASR", question_id)
        return evaluate("", question["text"], question["ideal_answer"], level)

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
        log.exception("grading failed for %s", question_id)
        raise HTTPException(502, f"Scoring failed: {e}") from e

    log.info(
        "graded %s level=%s in %.1fs — total=%d %s",
        question_id,
        level,
        time.perf_counter() - started,
        report["score"],
        {p: v["score"] for p, v in report["parameters"].items()},
    )
    return report
