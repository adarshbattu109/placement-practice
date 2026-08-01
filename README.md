# placement-practice

Spoken mock-interview practice. Pick a topic and an expertise level, record your answer out
loud, and get it transcribed and graded on **Completeness**, **Correctness** and **Relevance**
against the question's ideal answer.

Everything runs on the HuggingFace Inference API. **No model weights are downloaded** — the
whole thing installs in seconds and runs on a laptop.

## Quickstart

```bash
uv sync
cp .env.example .env.local     # then paste in your HF_TOKEN
uv run practice
```

Open <http://localhost:8000>, log in with any name and password, pick **Vocal**, choose a
topic and level, and start answering.

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
  └── POST /submit ──────────→ decode + silence gate
                                   ↓
                               ASR ─────────────────────→ openai/whisper-large-v3
                                   ↓ transcript
                               judge ───────────────────→ meta-llama/Llama-3.3-70B-Instruct
                                   ↓ 3 scores + notes
                               weight by level
  ←──────── JSON report ────────┘
```

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
  evaluate.py      the grader — levels, weights, judge prompt, parsing
  index.html       the entire frontend, one file, no build step
  questions.json   21 questions across 6 topics
poc_mic.py         standalone mic → transcript check, outside the web app
logs/              rotating server.log (gitignored)
```

## API

| Route | Purpose |
|-------|---------|
| `GET /` | The app |
| `GET /topics` | Available topics |
| `GET /questions?topic=` | Questions, optionally filtered |
| `GET /levels` | `["beginner", "advanced", "expert"]` |
| `POST /submit` | Multipart: `question_id`, `level`, `audio` (16 kHz mono WAV) → JSON report |

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
curl -F question_id=dsa-1 -F level=advanced -F audio=@/tmp/a.wav http://localhost:8000/submit
```

## Known limits

- **Login is a placeholder.** Any password works, nothing is stored server-side. Don't expose
  this server to an untrusted network — see [SECURITY.md](SECURITY.md).
- **Scores aren't reproducible.** The judge runs at `temperature=0.2`, so the same answer
  varies by a few points between runs.
- **No retry on judge failure.** A transient 502 from the provider loses that recording.
- **Nothing is persisted.** Close the tab and the report is gone.
- **Audio leaves your machine.** Recordings go to the HuggingFace Inference API for
  transcription. Don't speak anything confidential.

## License

[Apache-2.0](LICENSE)
