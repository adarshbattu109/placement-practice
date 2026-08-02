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
uv run python -m tests           # Every check that must pass before a push
uv run python -m tests api       # One check by name, while iterating
uv tool run ruff check .         # Lint
uv tool run ruff format .        # Format
```

`HF_TOKEN` and `SECRET_KEY` must both be in `.env.local` before the server starts —
`SECRET_KEY` signs the session cookie and there is no default, since a guessable key lets
anyone mint a session. See `.env.example`. The tests need neither a token nor the network.

## Architecture

- **`app/__init__.py`** — entrypoint and cross-cutting setup: `truststore.inject_into_ssl()`,
  `load_dotenv()`, `setup_logging()`, and `run()` which starts uvicorn. Nothing else imports
  from here except `setup_logging`.
- **`app/main.py`** — the FastAPI app and routes. Owns audio decoding, the silence gate, the
  ASR call, and the `current_user` dependency. Holds no grading logic and no SQL.
- **`app/evaluate.py`** — the grader. Owns the judge prompt, the backend seam, parsing and
  weighting. Knows nothing about HTTP, audio or FastAPI.
- **`app/index.html`** — the whole frontend, one file, no build step. Served by `GET /`
  from the same origin, so there is no CORS config to keep in sync.
- **`src/constants/`** — `constants.py` (models, `LEVELS`, auth and gate thresholds) and
  `filepaths.py` (every path, resolved once from the repo root).
- **`src/helpers/security.py`** — `scrypt` password hashing and `hmac` cookie signing. Stdlib.
- **`src/db/`** — `schema.sql` and `store.py`. **`store.py` is the only file that contains SQL.**
- **`data/questions.json`** — the seed for the question bank, loaded into SQLite on first
  run. The database is the source of truth after that; the JSON is not read again.
- **`tests/`** — the pre-push suite. `python -m tests` runs the module `demo()`s plus the
  API, frontend and packaging checks.

**Import direction:** `app.main` → `app.evaluate` → `src.*`. Nothing under `src/` imports
FastAPI or knows an HTTP request exists. Never the reverse.

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
- **Login required, signup open.** Every route except `GET /`, `POST /signup`, `POST /login`
  and `GET /levels` depends on `current_user`. `GET /levels` stays open because the signup
  form needs it before an account exists.
- **Sessions are signed, not stored.** The cookie is `user_id.issued_at.hmac`, so there is no
  sessions table. Rotating `SECRET_KEY` logs everyone out — that is the whole revocation
  story. ponytail: no server-side revocation, add a sessions table if single-device logout
  is ever needed.
- **The same reply for a wrong email and a wrong password.** `authenticate()` hashes a dummy
  password when the address is unknown so the timing matches too; a distinct "no such
  account" would let anyone enumerate registered addresses.
- **SQLite with WAL and STRICT.** `foreign_keys` is off by default in SQLite, so `connect()`
  turns it on — the `ON DELETE CASCADE`s in `schema.sql` are inert without it.
- **A sitting is opened before the first answer, closed after the last.** `POST /assessments`
  returns an id the browser threads through every `/submit`; `finish` averages the graded
  answers into `overall`. `finished_at IS NULL` is what makes a sitting writable, so
  `owns_assessment()` is the ownership *and* the open check, and `/submit` calls it before
  the ASR spend. `overall` and `finished_at` stay NULL until finish, so an abandoned sitting
  reads differently from a zero. `assessment_id` is optional — a `curl` submit still grades.
- **An ungraded answer is not a zero.** `finish_assessment()` averages `WHERE score IS NOT
  NULL`: a 502 from the judge is not the candidate's fault. Unanswered questions never reach
  `answers` at all. Only the browser's report screen counts an unrecorded answer as zero.
- **One row per question per sitting.** `UNIQUE (assessment_id, question_id)` plus an upsert,
  so a re-recorded answer replaces the take instead of being averaged in twice.

## Testing

Non-trivial logic leaves one runnable check behind, and everything runs from one command:
`uv run python -m tests`. It is wired into CI and into a `pre-push` pre-commit stage.

- Module self-checks — `demo()` in `security.py`, `store.py`, `evaluate.py`. Add to these
  rather than building a per-function suite.
- `tests/test_api.py` — every route through a `TestClient` against a temp database.
- `tests/test_frontend.py` — `index.html` has no build step, so a mistyped `$('id')` fails
  silently. This walks every id and every fetched route and checks they exist. Templated urls
  (`` `/assessments/${id}/finish` ``) are checked by shape, with the interpolation standing in
  for one path parameter.
- `tests/test_packaging.py` — builds the wheel and asserts it ships `questions.json`,
  `schema.sql` and `index.html`, and no local database.

No network in any test: the judge is stubbed and the only `/submit` path exercised is the
silence gate, which short-circuits before the ASR call.

## Don't

- **Don't download models.** No `transformers` pipelines running locally, no `torch`. If a
  feature needs local weights, it needs a discussion first.
- **Don't log transcripts or candidate audio.** Logs carry latency and scores only. Auth logs
  carry the user id, never the email or password.
- **Don't write SQL outside `src/db/store.py`.** Routes call functions, never a cursor.
- Don't commit `.env.local` or anything else holding `HF_TOKEN` or `SECRET_KEY`.
- Don't commit `data/practice.db` — it holds real accounts. Gitignored, and
  `tests/test_packaging.py` fails the build if it reaches a wheel.
- Don't remove `truststore.inject_into_ssl()` — without it every HF call fails behind a
  TLS-intercepting corporate proxy.
- Don't add runtime dependencies unless a few lines of stdlib genuinely can't do it.
- Don't use broad `except Exception` in new code except where a route must convert a remote
  failure into an HTTP status.
