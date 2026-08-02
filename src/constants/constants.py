"""Models, grading calibration and gate thresholds — the knobs worth tuning."""

# --- remote endpoints -----------------------------------------------------------
# Every model call in the app resolves through this table — there is no model name and no
# base url anywhere else. Each field is overridable per deployment by environment variable
# (`ASR_URL`, `ASR_MODEL`, `ASR_KEY`, and the same three for JUDGE and TTS), read at call
# time rather than import time because .env.local is loaded by app/__init__.py.
#
# An empty `url` means the HuggingFace Inference API. A url set means an OpenAI-shaped
# server, which is one wire format shared by Groq, OpenAI, vLLM, LM Studio and Ollama — so
# moving a role to another provider is two environment variables, not a code change.
# `src/helpers/inference.py` is what reads this; see it for the two call shapes.
#
# whisper-large-v3 handles Indian English well; the language is pinned so accented input is
# never mistaken for another one and silently translated.
ASR_LANGUAGE = "en"
ENDPOINTS = {
    # Timeouts are per role because the calls are nothing alike: ASR is ~3s on a minute of
    # speech, the judge is 20-60s, and Kokoro measured 72.9s for one sentence. All three
    # must stay under JOB_STALE_MINUTES below, or the watchdog re-queues live work.
    "asr": {"model": "openai/whisper-large-v3", "url": "", "timeout": 60},
    "judge": {"model": "meta-llama/Llama-3.3-70B-Instruct", "url": "", "timeout": 120},
    # The only text-to-speech model with a live provider on the Inference API: 82M params,
    # served by fal-ai. espnet/* and facebook/mms-tts-eng return no provider.
    "tts": {"model": "hexgrad/Kokoro-82M", "url": "", "timeout": 180},
}

# --- stored recordings ----------------------------------------------------------
# The browser sends 16 kHz mono WAV, which is 32 kB per second — a 21-question
# sitting is ~38 MB. Opus at the same rate is 9x smaller (measured: 1875 KiB of
# speech → 209 KiB), so takes are transcoded once before they are stored. The rate
# itself comes from the decoded upload, not from here.
#
# The window to challenge a transcription: long enough to notice a bad transcript and
# re-run ASR against the original, short enough that candidate speech is not kept
# indefinitely. ponytail: fixed retention for everyone, no per-user setting
RECORDING_RETENTION_DAYS = 10
# Nothing else caps the upload, and every stored byte is a byte on disk forever
# until the sweep runs. 10 minutes of 16 kHz WAV is ~19 MB.
MAX_UPLOAD_BYTES = 20 * 1024 * 1024

# --- the grading worker ---------------------------------------------------------
# One in-process thread polling the jobs table. At a few jobs a minute a queue service
# would be all cost and no benefit. ponytail: single worker, so a job waits up to one
# poll interval per stage; add a second thread only when the queue is measurably behind.
WORKER_POLL_SECONDS = 3
# ASR and the judge both fail transiently — 502, rate limit, a depleted balance. Three
# tries, then the job is parked with its error so the report can say what went wrong.
MAX_JOB_ATTEMPTS = 3
# A restart mid-job leaves the row 'running' with nobody working it, so anything claimed
# longer ago than this is assumed dead and re-queued. Must exceed every ENDPOINTS timeout,
# which is asserted at the bottom of this file.
JOB_STALE_MINUTES = 5

# --- generated speech -----------------------------------------------------------
# Opus only encodes at 8/12/16/24/48 kHz; 24k is transparent for a single voice.
TTS_OPUS_RATE = 24000

# --- judge ----------------------------------------------------------------------
# Sampling, not routing — the model and the url live in ENDPOINTS above.
JUDGE_MAX_TOKENS = 900
JUDGE_TEMPERATURE = 0.2

# --- auth -----------------------------------------------------------------------
# scrypt cost. n=2**14 is ~22ms here; OpenSSL's default 32 MB cap rejects anything
# above it unless maxmem is raised, which security.py does. Raise n when hardware
# makes it cheap — the parameters are stored per hash, so old ones keep working.
SCRYPT_COST = 2**14
SCRYPT_BLOCK = 8
SCRYPT_PARALLEL = 1
SCRYPT_DKLEN = 32
SCRYPT_SALT_BYTES = 16

COOKIE_NAME = "placemint_session"
COOKIE_MAX_AGE = 14 * 24 * 60 * 60  # two weeks
MIN_PASSWORD = 8

# --- silence gate ---------------------------------------------------------------
# Whisper hallucinates fluent text from a dead recording, so a quiet or very short
# take short-circuits to a zero report instead of reaching ASR.
MIN_SECONDS = 1.0
MIN_RMS = 0.005
# Below this, there is nothing to grade even if ASR returned words.
MIN_WORDS = 5

PARAMETERS = ("completeness", "correctness", "relevance")

# What each level is graded against, and how the three parameters are weighted for it.
# Weights must sum to 1 — asserted at import, so a bad edit fails loudly.
# ponytail: hand-set ratios, the calibration knob for the whole grader
LEVELS = {
    "beginner": {
        # Getting the fundamentals right matters most; breadth comes later.
        "weights": {"completeness": 0.20, "correctness": 0.55, "relevance": 0.25},
        "brief": (
            "The candidate is a BEGINNER. Expect the core idea in plain language and accurate "
            "fundamentals. Do not penalise missing edge cases, complexity analysis or production "
            "concerns. Reward a clear correct mental model. Grade generously on completeness."
        ),
    },
    "advanced": {
        # Balanced: knows the mechanism, now has to cover it and stay on point.
        "weights": {"completeness": 0.35, "correctness": 0.40, "relevance": 0.25},
        "brief": (
            "The candidate is ADVANCED. Expect the mechanism explained correctly plus tradeoffs, "
            "complexity, and at least one alternative approach with a reason to prefer it. "
            "Hand-waving over the hard part costs marks."
        ),
    },
    "expert": {
        # Correctness is assumed at this level, so depth and focus carry the score.
        "weights": {"completeness": 0.45, "correctness": 0.35, "relevance": 0.20},
        "brief": (
            "The candidate is an EXPERT. Expect precision, failure modes, edge cases and real "
            "production concerns, with the reasoning behind each choice. Penalise vagueness, "
            "textbook recitation without judgement, and any imprecise claim. Grade strictly."
        ),
    },
}

for _name, _spec in LEVELS.items():
    assert set(_spec["weights"]) == set(PARAMETERS), f"{_name}: weights must cover {PARAMETERS}"
    assert abs(sum(_spec["weights"].values()) - 1) < 1e-9, f"{_name}: weights must sum to 1"

# A timeout longer than the watchdog window means the worker is still waiting on a call the
# watchdog has already declared dead, so the same job runs twice. Caught here rather than in
# production, where it looks like a duplicate charge and nothing else.
for _role, _spec in ENDPOINTS.items():
    assert _spec.keys() == {"model", "url", "timeout"}, f"{_role}: unexpected endpoint fields"
    assert _spec["timeout"] < JOB_STALE_MINUTES * 60, f"{_role}: timeout exceeds the stale window"
