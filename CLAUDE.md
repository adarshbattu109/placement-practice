# CLAUDE.md — Development Conventions for placement-practice

## Project Overview

**Placemint.Academy** is a spoken mock-interview trainer. The browser records an answer, the
server transcribes it and an LLM judge grades it on Completeness, Correctness and Relevance
against the question's ideal answer, weighted by the candidate's stated expertise level.

The product is `Placemint.Academy`; the repo, the package and the `practice` script stay
`placement-practice`. The name belongs in user-facing copy — the page title, the wordmark, the
report header, the FastAPI title — and nowhere in an import path or a distribution name.
`COOKIE_NAME` is `placemint_session`; it was renamed with the product, which signed out every
existing session once.

Every model call is remote — **no model weights are ever downloaded**. The HuggingFace
Inference API is the default for all three roles; `ENDPOINTS` in `src/constants/constants.py`
is where any of them is pointed somewhere else.

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
  `lifespan` that runs the worker, and the `current_user` dependency. Holds no grading logic
  and no SQL.
- **`app/evaluate.py`** — the grader. Owns the judge prompt, parsing and weighting. Knows
  nothing about HTTP, audio, FastAPI, or which model answers.
- **`app/worker.py`** — the background thread that drains `jobs`: ASR, then the judge, then
  re-average the sitting. Owns both remote calls' retry policy and the hourly recording
  sweep. Knows nothing about HTTP; it reads and writes the same tables the routes do.
- **`app/index.html`** — the whole frontend, one file, no build step. Served by `GET /`
  from the same origin, so there is no CORS config to keep in sync.
- **`src/constants/`** — `constants.py` (`ENDPOINTS`, `LEVELS`, auth and gate thresholds) and
  `filepaths.py` (every path, resolved once from the repo root).
- **`src/helpers/inference.py`** — every remote model call and the client construction behind
  it. `chat()` and `transcribe()` take a role name, not a model or a url. **The only file that
  builds an inference client.**
- **`src/helpers/security.py`** — `scrypt` password hashing and `hmac` cookie signing. Stdlib.
- **`src/db/`** — `schema.sql` and `store.py`. **`store.py` is the only file that contains SQL.**
- **`data/questions.json`** — the seed for the question bank, loaded into SQLite on first
  run. The database is the source of truth after that; the JSON is not read again.
- **`tests/`** — the pre-push suite. `python -m tests` runs the module `demo()`s plus the
  API, frontend and packaging checks.

**Import direction:** `app.main` → `app.worker` → `app.evaluate` → `src.*`. Nothing under
`src/` imports FastAPI or knows an HTTP request exists. Never the reverse.

**One config table for every endpoint.** `ENDPOINTS` in `src/constants/constants.py` holds
`{model, url, timeout}` per role — `asr`, `judge`, `tts` — and every field is overridable by
`<ROLE>_MODEL`, `<ROLE>_URL`, `<ROLE>_KEY`, `<ROLE>_TIMEOUT`. A model name or a base url
anywhere else in the tree is a bug. Empty `url` means HuggingFace; a url set means any
OpenAI-shaped server (Groq, OpenAI, vLLM, LM Studio, Ollama), which is one wire format.

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
  as judge with `response_format={"type": "json_object"}`. Both are defaults in `ENDPOINTS`,
  not literals at the call site.
- **`chat()` is one call, `transcribe()` has to branch.** Both backends speak
  chat-completions, and `InferenceClient` takes a `base_url`, so the judge needs no branch at
  all. ASR genuinely differs: HuggingFace takes raw bytes on its own client, everyone else
  takes a multipart upload at `POST /audio/transcriptions`. The upload is named `take.wav` or
  `take.ogg` by sniffing the header — both providers read the container off the extension.
- **Every endpoint timeout must stay under `JOB_STALE_MINUTES`.** Asserted at import in
  `constants.py`. A longer timeout means the worker is still waiting on a call the watchdog
  has already declared dead, so the job runs twice — which looks like a duplicate charge and
  nothing else.
- **Nothing slow happens in the request.** `/submit` stores the take, queues a job and
  answers **202** `{question_id, queued, report}`; ASR and the judge both run in the worker
  and the browser polls. Two cases still come back graded inline with `queued: false` — a
  silence-gate trip (the zero is local) and a submit with no `assessment_id` (nothing would
  poll for it, and `curl -F question_id=… /submit` stays a one-command check of the chain).
- **One job row per take, not per stage.** `jobs.kind` advances `transcribe` → `grade`, so
  `UNIQUE (assessment_id, question_id)` mirrors `answers` and `recordings` and a re-submit of
  an open sitting resets the work instead of racing a duplicate job. `attempts` resets at each
  stage — a flaky ASR call must not spend the judge's retries.
- **Claiming is one atomic `UPDATE … RETURNING`.** SQLite serializes writes, so there is no
  need for `SKIP LOCKED` and no need for a lock in Python. A crash mid-job leaves the row
  `running`, which `requeue_stale_jobs()` picks up after `JOB_STALE_MINUTES`.
- **`rescore_assessment()` is not `finish_assessment()`.** `finish` closes the sitting and
  must be a no-op on a second call; `rescore` runs after every verdict so `overall` climbs as
  answers land. A sitting can be closed and still be grading.
- **`history()` lists open sittings too.** It used to filter `finished_at IS NOT NULL`, which
  made an interview abandoned halfway look like it had never happened — and the dashboard is
  the only route back to one. `finished_at IS NULL` is what marks a row open. Listed is not
  resumable: the questions and the recordings only ever lived in the tab that started it.
- **`history()` returns `graded`, and the browser must not derive it.** `count(n.score)`,
  because the old `total - working - stuck` arithmetic counted a question that was never
  recorded — no `answers` row, no `jobs` row — as graded. `ORDER BY started_at DESC, id DESC`:
  `started_at` is second-resolution, so two sittings opened in the same second need the tiebreak.
- **Three nav levels, three different shapes.** Home is full-width hairline rows (`.bay`), the
  formats are cards (`.mode`), the interview is the level ladder. Reusing the cards for home
  would have made two consecutive nav levels look identical. Each home row carries a live
  readout drawn from `/history` and `/questions`, so it says what is waiting before a click.
- **`assessment()` returns a `jobs` key.** A take awaiting transcription has no `answers` row
  at all, so without it the report page would show a queued question as unanswered. The
  `working`/`stuck` counts on `history()` are correlated subqueries, not a second `LEFT JOIN`
  — the `answers` join already fans the row out and would multiply them.
- **Silence gate before ASR.** Whisper hallucinates fluent text from a dead recording, so
  `/submit` checks length and RMS and short-circuits to a zero report instead of queueing it.
- **The take is stored before anything can fail.** `_keep()` transcodes to Opus and writes
  `recordings` *before* the silence gate, so a gate trip, an ASR 502 or a judge 502 all leave
  the audio behind to re-run from. Opus because it is 9× smaller than the WAV the browser
  sends (measured: 1875 KiB → 209 KiB per minute), which is what makes retention affordable;
  `soundfile` encodes it in-process, so this still needs no ffmpeg. It runs in the threadpool
  — ~1s of CPU per minute of audio would otherwise block the event loop.
- **Audio expires, transcripts don't.** `RECORDING_RETENTION_DAYS` is the window to
  challenge a transcription. `prune_recordings()` runs from the worker's idle branch, at most
  once an hour. Scores and transcripts are the product and have no expiry. This reverses the
  original spec decision — see the amendment note in the design doc before arguing it back.
- **A separate `recordings` table, never a column on `answers`.** A ~200 kB blob on the row
  the history page reads would pull audio into every history query and rewrite the whole
  overflow chain into the WAL on each re-take.
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
- **One row per question per sitting.** `UNIQUE (assessment_id, question_id)` plus an upsert
  on `answers`, `recordings` and `jobs`, so a re-submit replaces the take instead of being
  averaged in twice. That upsert is a **retry**, not a retake: a sitting is only writable
  while `finished_at IS NULL`, and once it is finished there is no second attempt at it —
  start a new sitting, the old one stays as it was.

## Testing

Non-trivial logic leaves one runnable check behind, and everything runs from one command:
`uv run python -m tests`. It is wired into CI and into a `pre-push` pre-commit stage.

- Module self-checks — `demo()` in `security.py`, `inference.py`, `store.py`, `evaluate.py`,
  `worker.py`. Add to these rather than building a per-function suite. `worker.demo()` stubs
  both remote calls and drives the queue with `step()` rather than a thread, so it needs no
  clock. `inference.demo()` covers the routing table and both call shapes with stub clients,
  so a provider swap is checked without a request; it saves and restores every `<ROLE>_*`
  variable it touches, since the suite shares one process.
- `tests/test_api.py` — every route through a `TestClient` against a temp database.
- `tests/test_frontend.py` — `index.html` has no build step, so a mistyped `$('id')` fails
  silently. This walks every id and every fetched route and checks they exist. Templated urls
  (`` `/assessments/${id}/finish` ``) are checked by shape, with the interpolation standing in
  for one path parameter.
- `tests/test_packaging.py` — builds the wheel and asserts it ships `questions.json`,
  `schema.sql` and `index.html`, and no local database.

No network in any test: the judge is stubbed everywhere, and the two `/submit` paths
exercised both stop short of a remote call — the silence gate short-circuits, and a loud take
queues a job that no test lets a worker claim.

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
