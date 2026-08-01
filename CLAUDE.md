# CLAUDE.md — Development Conventions for placement-practice

## Project Overview

placement-practice is a spoken mock-interview trainer. The browser records an answer, the
server transcribes it and an LLM judge grades it on Completeness, Correctness and Relevance
against the question's ideal answer, weighted by the candidate's stated expertise level.

Everything runs on the HuggingFace Inference API — **no model weights are ever downloaded**.

## Build & Run

```bash
uv sync                          # Install all dependencies (incl. dev group)
uv run practice                  # Start the server on http://localhost:8000
uv run python -m app.evaluate    # Self-check: parsing + weighting, judge stubbed out
uv tool run ruff check .         # Lint
uv tool run ruff format .        # Format
```

`HF_TOKEN` must be in `.env.local` before the server can call the Inference API. The
self-check does not need it — the judge is stubbed.

## Architecture

- **`app/__init__.py`** — entrypoint and cross-cutting setup: `truststore.inject_into_ssl()`,
  `load_dotenv()`, `setup_logging()`, and `run()` which starts uvicorn. Nothing else imports
  from here except `setup_logging`.
- **`app/main.py`** — the FastAPI app and routes. Owns audio decoding, the silence gate, and
  the ASR call. Holds no grading logic.
- **`app/evaluate.py`** — the grader. Owns `LEVELS`, the judge prompt, parsing and weighting.
  Knows nothing about HTTP, audio or FastAPI.
- **`app/index.html`** — the whole frontend, one file, no build step. Served by `GET /`
  from the same origin, so there is no CORS config to keep in sync.
- **`app/questions.json`** — the question bank. Each entry: `id`, `topic`, `text`,
  `ideal_answer`.

**Import direction:** `main` → `evaluate`. Never the reverse.

## Code Style

- Python 3.14, type hints on route signatures and public helpers
- Line length: 100 characters
- Formatter/linter: Ruff (configured in pyproject.toml)
- Docstrings: one-line summary on modules and public functions
- Comments: only when the "why" is non-obvious — never describe what the code does
- Mark deliberate simplifications with a `ponytail:` comment naming the ceiling and the
  upgrade path, e.g. `# ponytail: flat RMS gate, no VAD`

## Key Patterns

- **Cloud-only inference.** `openai/whisper-large-v3` for ASR (language pinned to `en` so
  accented English is transcribed, never translated), `meta-llama/Llama-3.3-70B-Instruct`
  as judge with `response_format={"type": "json_object"}`.
- **Silence gate before ASR.** Whisper hallucinates fluent text from a dead recording, so
  `/submit` checks length and RMS and short-circuits to a zero report instead of calling it.
- **Weighted scoring.** Each level in `LEVELS` carries a `weights` dict over the three
  parameters that must sum to 1 — asserted at import so a bad edit fails loudly. This is the
  calibration knob for the whole grader; expect to tune it.
- **Level affects grading twice.** The level `brief` shifts the judge's raw scores, and the
  weights then amplify the shift. Advanced and expert barely separate on raw scores — the
  weights are what pull them apart.
- **Browser does the audio work.** `index.html` resamples to 16 kHz mono and encodes WAV by
  hand, so the server needs no ffmpeg.
- **Logging** goes to `logs/server.log` (rotating, 2 MB × 5) and the console. `setup_logging()`
  is idempotent and uvicorn runs with `log_config=None` so it cannot replace our handlers.

## Testing

Non-trivial logic leaves one runnable check behind. `app/evaluate.py` has `demo()`, run via
`uv run python -m app.evaluate`, which stubs the remote judge and asserts the parser clamps
scores and each level weights them correctly. Add to it rather than building a suite.

## Don't

- **Don't download models.** No `transformers` pipelines running locally, no `torch`. If a
  feature needs local weights, it needs a discussion first.
- **Don't log transcripts or candidate audio.** Logs carry latency and scores only.
- Don't commit `.env.local` or anything else holding `HF_TOKEN`.
- Don't remove `truststore.inject_into_ssl()` — without it every HF call fails behind a
  TLS-intercepting corporate proxy.
- Don't add runtime dependencies unless a few lines of stdlib genuinely can't do it.
- Don't use broad `except Exception` in new code except where a route must convert a remote
  failure into an HTTP status.
