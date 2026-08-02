"""Models, grading calibration and gate thresholds — the knobs worth tuning."""

# --- speech ---------------------------------------------------------------------
# whisper-large-v3 handles Indian English well; pin the language so accented
# input is never mistaken for another one and silently translated.
ASR_MODEL = "openai/whisper-large-v3"
ASR_OPTIONS = {"language": "en", "task": "transcribe"}

# The only text-to-speech model with a live provider on the Inference API: 82M
# params, served by fal-ai. espnet/* and facebook/mms-tts-eng return no provider.
TTS_MODEL = "hexgrad/Kokoro-82M"
# Opus only encodes at 8/12/16/24/48 kHz; 24k is transparent for a single voice.
TTS_OPUS_RATE = 24000

# --- judge ----------------------------------------------------------------------
# The default routes to the HF Inference API. Set JUDGE_URL to http://localhost:11434/v1
# for Ollama or https://api.openai.com/v1 for OpenAI, with JUDGE_MODEL to match — all
# three speak the same chat-completions shape, so the calling code does not change.
# Read from the environment at call time, not import time, because .env.local is loaded
# by app/__init__.py and this module must not care who imports it first.
JUDGE_MODEL_DEFAULT = "meta-llama/Llama-3.3-70B-Instruct"
JUDGE_MAX_TOKENS = 900
JUDGE_TEMPERATURE = 0.2
JUDGE_TIMEOUT = 120

# --- auth -----------------------------------------------------------------------
# scrypt cost. n=2**14 is ~22ms here; OpenSSL's default 32 MB cap rejects anything
# above it unless maxmem is raised, which security.py does. Raise n when hardware
# makes it cheap — the parameters are stored per hash, so old ones keep working.
SCRYPT_COST = 2**14
SCRYPT_BLOCK = 8
SCRYPT_PARALLEL = 1
SCRYPT_DKLEN = 32
SCRYPT_SALT_BYTES = 16

COOKIE_NAME = "booth_session"
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
