# placement-practice

Spoken mock-interview practice. Pick a topic and an expertise level, record your answer out
loud, and get it transcribed and graded on **Completeness**, **Correctness** and **Relevance**
against the question's ideal answer.

Everything runs on the HuggingFace Inference API. **No model weights are downloaded** — the
whole thing installs in seconds and runs on a laptop.

## Quickstart

```bash
uv sync
cp .env.example .env.local     # then fill in HF_TOKEN and SECRET_KEY
uv run practice
```

Open <http://localhost:8000>, **create an account**, pick **Vocal**, choose a topic and
level, and start answering. Signup is open to anyone who can reach the server.

`SECRET_KEY` signs the session cookie and has no default — generate one with:

```bash
python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Changing it later logs everyone out. Accounts and the question bank live in
`data/practice.db`, created and seeded on first run.

### Getting an HF token

Create one at [hf.co/settings/tokens](https://huggingface.co/settings/tokens) with the
**"Make calls to Inference Providers"** permission, then put it in `.env.local`:

```
HF_TOKEN=hf_xxxxxxxxxxxxxxxxxxxxx
```

The free community tier is enough. A 20-question session costs roughly $0.15 of included
credit — see [Cost](#cost).

## How it works

```
browser                        server                      HuggingFace Inference API
───────                        ──────                      ─────────────────────────
MediaRecorder
  ↓ resample 16 kHz mono
  ↓ encode WAV
  └── POST /submit ──────────→ keep the take as Opus
                                   ↓
                               silence gate
                                   ↓
                               queue one job
  ←──────── 202 queued ─────────┘

                               grading worker (background thread)
                                   ↓ every 3s: claim one job
                               ASR ─────────────────────→ openai/whisper-large-v3
                                   ↓ transcript
                               judge ───────────────────→ meta-llama/Llama-3.3-70B-Instruct
                                   ↓ 3 scores + notes
                               weight by level, store, re-average the sitting
  GET /history (poll) ────────────→ ↓
  ←──────── the queue draining ────┘
```

Nobody waits on a spinner: ASR is ~3s and the judge is 20–60s, so a full sitting would be
twenty minutes of staring. `/submit` returns as soon as the take is stored, and the history
page polls until the scores land.

The browser does the audio work — resampling and WAV encoding happen in
`AudioContext`/`OfflineAudioContext`, so the server needs no ffmpeg.

A **silence gate** sits in front of ASR. Whisper hallucinates fluent text from a dead
recording (a real 3-second silence transcribed as *"Thank you very much."* ten times over),
so a recording under a second or below an RMS of 0.005 short-circuits to a zero report
instead of being sent.

## Grading

The judge scores three parameters from 0–100:

| Parameter | What it measures |
|-----------|------------------|
| **Completeness** | How much of the ideal answer's substance you covered |
| **Correctness** | Whether what you said is technically true. Confident wrong claims score low here even if they're on-topic, and are itemised back to you |
| **Relevance** | Whether you answered the question asked, without padding or drifting |

### Expertise level

The level you pick changes your score **twice**, and the two compound:

1. **The bar moves.** Each level sends the judge a different brief. A beginner is not
   penalised for missing edge cases; an expert is penalised for vagueness and for textbook
   recitation without judgement.
2. **The weights change.** Each level weights the three parameters differently, and the
   weights always sum to 1 (asserted at import, so a bad edit fails loudly).

| Level | Completeness | Correctness | Relevance | Rationale |
|-------|--------------|-------------|-----------|-----------|
| Beginner | 0.20 | **0.55** | 0.25 | Getting the fundamentals right matters most; breadth comes later |
| Advanced | 0.35 | 0.40 | 0.25 | Knows the mechanism — now has to cover it and stay on point |
| Expert | **0.45** | 0.35 | 0.20 | Correctness is assumed, so depth and focus carry the score |

The same spoken answer scored **90 as a beginner and 55 as an expert** in testing. Note that
advanced and expert barely separate on the judge's raw scores — the weights are what pull
them apart. `LEVELS` in `app/evaluate.py` is the calibration knob; expect to tune it.

## Modes

| Mode | Question | Answer | Status |
|------|----------|--------|--------|
| **Vocal** | Shown as text | Recorded, then transcribed and graded | Available — free tier |
| **Telephonic** | Spoken aloud | Transcribed live as you speak | Not built |
| **Video** | Conversational, on camera | Natural back-and-forth | Not built |

## Cost

Measured on a real run: **817 tokens per question** (622 in, 195 out) for the judge, plus one
ASR call. A 20-question session lands around **$0.15** of included credit, dominated by ASR
rather than the judge.

To confirm you're on the free tier, check `isPro` and `canPay` at
<https://huggingface.co/api/whoami-v2>. With `canPay: false` you cannot be charged — calls
return HTTP 402 once the monthly credit is exhausted, and the allowance resets monthly.

## Project layout

```
app/
  __init__.py      entrypoint (uv run practice), TLS trust store, .env, logging
  main.py          FastAPI app and routes
  evaluate.py      the grader — judge prompt, parsing, weighting
  worker.py        the background thread that drains the grading queue
  index.html       the entire frontend, one file, no build step
src/
  constants/       models, levels and weights, thresholds, every filepath
  helpers/         security.py — scrypt passwords, hmac session cookies
  db/              schema.sql and store.py, the only file containing SQL
data/
  questions.json   21 questions across 6 topics — the seed for a fresh database
  practice.db      accounts, the live question bank, past interviews (gitignored)
tests/             the pre-push suite: uv run python -m tests
logs/              rotating server.log (gitignored)
```

## API

Everything except `GET /`, `POST /signup`, `POST /login` and `GET /levels` needs a session
cookie and returns 401 without one.

| Route | Purpose |
|-------|---------|
| `GET /` | The app |
| `POST /signup` | Multipart: `email`, `name`, `password`, `level` → profile, signs you in |
| `POST /login` | Multipart: `email`, `password` → profile |
| `POST /logout` | Clears the session cookie |
| `GET /me` | The signed-in profile, or 401 |
| `GET /topics` | Available topics |
| `GET /questions?topic=` | Questions, optionally filtered |
| `GET /levels` | `["beginner", "advanced", "expert"]` |
| `POST /assessments` | Multipart: `level`, `questions`, `topic` → `{"id": n}`, an open sitting |
| `POST /submit` | Multipart: `question_id`, `level`, `audio` (16 kHz mono WAV), `assessment_id` → **202** `{question_id, queued, report}` |
| `POST /assessments/{id}/finish` | Closes the sitting → `{id, overall, working}`. Idempotent, and it does not wait for the queue |
| `GET /history` | Your sittings, newest first, each with `working` and `stuck` counts |
| `GET /assessments/{id}` | One sitting with every answer and every unfinished job. 404 for anyone else's |

`/submit` answers 202, not 200: the answer is accepted, not graded. `queued: true` means the
report is `null` and the worker has it — poll `/history` or `/assessments/{id}`.

Two cases come back graded on the spot instead, with `queued: false` and a report attached:
a recording that trips the silence gate (the zero is local, and spending an ASR call on a
dead recording is what the gate exists to prevent), and a submit with no `assessment_id`
(there is no sitting to queue against and nobody to poll, so `curl -F question_id=…` stays a
one-command check of the whole chain).

A report looks like:

```json
{
  "transcript": "a hash map hashes the key to a bucket ...",
  "score": 66,
  "level": "advanced",
  "weights": {"completeness": 0.35, "correctness": 0.4, "relevance": 0.25},
  "parameters": {
    "completeness": {"score": 90, "note": "..."},
    "correctness": {"score": 30, "note": "...", "errors": ["said O(n) for average lookup"]},
    "relevance": {"score": 90, "note": "..."}
  },
  "errors": ["said O(n) for average lookup"],
  "comments": {"strengths": ["..."], "gaps": ["..."], "suggestion": "..."}
}
```

## Development

```bash
uv run python -m app.evaluate    # self-check: parsing + weighting, judge stubbed
uv run python -m app.worker      # self-check: the queue drains, failures park
uv tool run ruff check .
uv tool run ruff format .
```

The self-check needs no token. See [CONTRIBUTING.md](CONTRIBUTING.md) for conventions and
[CLAUDE.md](CLAUDE.md) for architecture rules.

### Behind a corporate proxy

`truststore.inject_into_ssl()` runs at import so Python uses the OS trust store. Without it,
every HF call fails with `CERTIFICATE_VERIFY_FAILED: self-signed certificate in chain` on a
machine with a TLS-intercepting proxy — even though `curl` works. Don't remove it.

### Testing without a microphone

```bash
say -o /tmp/a.aiff "a hash map hashes the key to a bucket index"
afconvert -f WAVE -d LEI16@16000 -c 1 /tmp/a.aiff /tmp/a.wav
curl -c /tmp/jar -F email=me@example.com -F name=Me -F password=devpassword \
  http://localhost:8000/signup
curl -b /tmp/jar -F question_id=dsa-1 -F level=advanced -F audio=@/tmp/a.wav \
  http://localhost:8000/submit
```

### Running the checks

```bash
uv run python -m tests          # everything: self-checks, routes, frontend, packaging
uv run python -m tests api      # one check by name
```

No test touches the network, so none of them need `HF_TOKEN`.

## Known limits

- **Scores aren't reproducible.** The judge runs at `temperature=0.2`, so the same answer
  varies by a few points between runs.
- **A job gives up after three attempts.** ASR and the judge both fail transiently, so the
  worker retries; past that the answer is parked with its error and the sitting reads as
  partly graded. The take is still stored, so the fix is re-running it, not re-recording.
- **One worker thread.** A job waits up to one 3-second poll per stage, and a sitting is
  graded one answer at a time — a 20-question sitting takes 20 × ~25s to come back.
- **No password reset,** and no way to revoke one session — rotating `SECRET_KEY` signs
  everyone out at once. Don't expose this server to an untrusted network; see
  [SECURITY.md](SECURITY.md).
- **Audio leaves your machine.** Recordings go to the HuggingFace Inference API for
  transcription. Don't speak anything confidential.

## License

[Apache-2.0](LICENSE)
