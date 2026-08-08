# MCQ design — a fourth format, graded without a model

Date: 2026-08-08
Status: approved, not yet implemented

## What this adds

A fourth interview format alongside Vocal, Telephonic and Video. The candidate reads a
question, picks one of four options, and the whole set is marked at the end. **No ASR and
no judge run at any point** — grading is one integer comparison, so the answer is right or
wrong the instant it is submitted.

Free tier, like Vocal. It still requires an account: every sitting attaches to a `user_id`,
appears in `/history`, and nothing in the auth model changes. Telephonic and Video stay
disabled and paid.

The point of the format is an on-ramp. Vocal asks a candidate to talk for a minute into a
microphone before they know whether the product is any good; MCQ asks them to click four
times. Everything downstream — history, the report screen, the score — is the same machinery.

## Decisions and why

### MCQ questions are rows in `questions`, and the FKs are why

Three tables reference the bank: `answers.question_id`, `recordings.question_id` and
`jobs.question_id` are all `REFERENCES questions(id)`. Scores and transcripts work exactly
as they do for Vocal, which means an MCQ answer is a row in `answers` — so an MCQ question
must exist in `questions` or the foreign key rejects the insert.

The alternative was a parallel `mcq_answers` table, and it was rejected outright: it would
duplicate `finish_assessment()`, `rescore_assessment()`, the `graded` count in `history()`
and the report assembly in `assessment()`. Four aggregates reimplemented so one column could
be avoided is the wrong trade.

`questions.ideal_answer` is NOT NULL and holds the correct option's text. That is not a
filler value — for an MCQ, the correct option *is* the ideal answer.

### The options live in a satellite table, not in nullable columns

`mcq` is keyed on `question_id` with every field NOT NULL, so an MCQ is either completely
specified or absent. The rejected alternative was four nullable columns on `questions`
(`options`, `correct`, `explanation`, `level`), which permits a half-generated row —
`options` written, `correct` still NULL — in a schema that is otherwise STRICT everywhere
and asserts its invariants loudly at import.

This mirrors `recordings` being its own table rather than a column on `answers`, but **not
for the same reason**: that split was about a ~200 kB blob dragging audio into every history
query, and a ~300-byte options array is not that. The argument here is invariant
enforcement alone. Worth saying plainly so nobody later reads a size rationale into it.

```sql
CREATE TABLE IF NOT EXISTS mcq (
  question_id TEXT PRIMARY KEY REFERENCES questions(id) ON DELETE CASCADE,
  source_id   TEXT NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
  level       TEXT NOT NULL,
  options     TEXT NOT NULL,    -- JSON array of exactly 4 strings
  correct     INTEGER NOT NULL, -- 0-3, an index into options
  explanation TEXT NOT NULL,
  created_at  TEXT NOT NULL DEFAULT (datetime('now'))
) STRICT;

CREATE INDEX IF NOT EXISTS mcq_by_level ON mcq(level);
```

`source_id` earns its place twice: the build step's skip-what-exists check is "does this
source already have three rows", and editing or deleting a source question cascades to its
MCQs instead of orphaning them.

### Two discriminator columns, added by guarded migration

`questions.format` (`'vocal'` default, `'mcq'`) so the Vocal flow filters MCQ rows out of
`GET /questions` with a column test rather than a `LEFT JOIN … IS NULL`.
`assessments.format` so the report screen knows how to render itself and `/history` can
label a row.

**`schema.sql` is re-run on every open and SQLite has no `ADD COLUMN IF NOT EXISTS`.** The
`CREATE TABLE` above is idempotent; the two columns are not. `store.setup()` reads
`PRAGMA table_info` and issues the `ALTER` only when the column is absent. This is the first
migration in the project, so it is worth doing carefully — a bare `ALTER` in `schema.sql`
would throw on the second open and take the whole app down with it.

### Level selects difficulty, because weights are meaningless here

For Vocal, the level shifts the judge's prompt and reweights three parameters. Neither
exists for a right/wrong answer, so `LEVELS[level]["weights"]` has nothing to weight and
`LEVELS[level]["brief"]` has no judge to brief.

Instead the level picks *which questions you get*. Each MCQ carries a `level`, and a sitting
serves only matching rows. The expertise dial keeps its meaning and its screen, and the score
stays a plain percentage. `LEVELS` is untouched — MCQ reads the keys for validation and
ignores the values.

**A sitting serves every MCQ matching the chosen topic and level**, which is the same rule
the Vocal flow uses for its bank. With 21 per level spread across the topics, a single-topic
sitting is a handful of questions and an all-topics sitting is 21. If that turns out to be
the wrong length in practice it is a `LIMIT` in one query, not a design change.

### Three MCQs per source question, one per level

21 source questions produce **63 MCQs**: for each source, a beginner, an advanced and an
expert variant probing the same concept at increasing depth. One-per-source was rejected
because auto-tagging difficulty leaves the levels unevenly filled — a level with four
questions is not a sitting.

### Generation is a build step, and its output is reviewed in a diff

`src/helpers/mcq.py`, run by hand as `python -m src.helpers.mcq`, exactly like
`src/helpers/speech.py`. It reads each source question's `text` and `ideal_answer`, calls
`inference.chat()`, and writes to **`data/mcq.json`**, which is committed.

Writing to a committed JSON file rather than straight to the database is the load-bearing
choice here, for one reason: **an LLM-authored answer key grades real people.** A distractor
that is quietly also correct marks a right answer wrong, and the candidate has no recourse —
there is no judge to argue with and no transcript to re-read. Putting the generated bank in
a file means the answer key arrives as a reviewable diff before it can mark anyone.

It also matches the audio precedent for a second reason: `data/audio/<voice>/<id>.opus` is
committed and shipped in the wheel because an installed copy cannot re-run the build step
without a token. The same is true of MCQs, so `data/mcq.json` ships too.

`data/mcq.json` is a **seed**, like `data/questions.json` — loaded on a fresh database and
never read again. The database is the source of truth after that. Seeding one entry writes
**two rows**: the question itself into `questions` (with `format='mcq'`, and the correct
option's text as `ideal_answer`), and its payload into `mcq`. Both happen in the same
transaction, so a half-seeded MCQ is not reachable.

**The generation reuses the `judge` role in `ENDPOINTS`, not a new one.** It is the same
chat-completion call shape, and the v2 design already set this precedent for slice 8's
question generation: "Generation reuses the existing judge seam, so it runs against Ollama
in dev and the cloud model for a demo, with no new client." A fourth role would be a config
knob with no second value.

Re-running skips any source that already has three entries, so adding a topic later
generates only the new questions. Same property as `speech.generate()`, same reason.

### Generated output is validated before it is written

The model is asked for JSON and does not always comply. Every generated entry is checked
before it reaches the file, and a failure is counted and skipped rather than aborting the
run — a 402 partway through must not lose the work already done:

- exactly 4 options, all non-empty and mutually distinct after whitespace normalisation
- `correct` an integer in 0-3
- `explanation` non-empty
- `level` one of `LEVELS`
- the question stem non-empty and not identical to the source question

Distinctness matters more than it looks: two options that differ only by punctuation make a
question unanswerable, and it is exactly the failure a fluent model produces.

## Flow

| Step | Endpoint | Does | Latency |
|---|---|---|---|
| 1 | `POST /assessments` | opens the sitting with `format='mcq'` | instant |
| 2 | `GET /questions?format=mcq&level=…` | the set for this level and topic | instant |
| 3 | `POST /assessments/{id}/mcq` | mark one answer, write the `answers` row | instant |
| 4 | `POST /assessments/{id}/finish` | average the marks into `overall` | instant |
| 5 | `GET /assessments/{id}` | the report, with every pick and explanation | instant |

Step 3 takes `{question_id, choice}` and returns **201 `{recorded: true}`** — deliberately
*not* whether the answer was right, because feedback is end-only and the browser must not be
able to reveal it early. `owns_assessment()` gates it exactly as `/submit` does, so a closed
sitting rejects writes and one candidate cannot write into another's sitting.

`POST /submit` stays audio-only and is not touched. No `jobs` row and no `recordings` row is
ever created for an MCQ sitting; the worker never sees one. `assessment()`'s `jobs` key comes
back empty, and `history()`'s `graded` count works unchanged because it is `count(n.score)`.

Unlike `/submit`, this route has **no anonymous variant**. `/submit` accepts a missing
`assessment_id` so `curl -F question_id=… /submit` stays a one-command check of the ASR and
judge chain; MCQ has no remote chain to check, so requiring the sitting removes a branch that
would buy nothing.

## Grading

One new pure function in `app/evaluate.py`, beside `evaluate()`:

```python
def grade_choice(question, choice): ...
```

It knows nothing about HTTP, matching the file's existing contract. It returns the same
report shape the Vocal path produces, so the report screen renders one structure:

- `score` — 100 or 0. `finish_assessment()` averages `WHERE score IS NOT NULL`, so `overall`
  is the percentage correct for free, with no new aggregate.
- `transcript` — the chosen option's text. The same column meaning as Vocal: what the
  candidate conveyed.
- `report` — JSON carrying `chosen`, `correct`, `options`, `explanation` and `level`, which
  is everything the review needs.

A `choice` outside 0-3 is a `ValueError`, converted to a 400 by the route. An unanswered
question never reaches `answers` at all, exactly as in Vocal, and only the browser's report
screen counts it as zero.

## Frontend

`app/index.html`, one file, no build step.

- **A fourth `.mode` card**, `id="mode-mcq"`, tier Free. The `.modes` grid is already
  `repeat(auto-fit, minmax(15rem, 1fr))`, so four cards reflow with no CSS change.
- **The lede changes.** "One is ready now. The other two need a paid tier for live streaming
  and video" is now wrong in both halves — two are ready, two are paid.
- **A new `view-mcq` section** for the sitting. Branching the recorder view on format would
  tangle the microphone lifecycle with a radio group for no gain; a separate section is
  smaller and cannot break Vocal. An MCQ sitting shows one question at a time, options as a
  radio group, and a Next button — no recorder, no timer, no audio permission prompt.
- **The report screen is reused**, branching on `format` to render the option review instead
  of the three-parameter bars.
- **`leave()` must guard MCQ too.** An MCQ sitting abandoned mid-way is stuck at "started"
  forever in exactly the way the v2 bug was — only `POST /finish` closes a sitting. This is
  a known, already-shipped-once failure, so it is a requirement, not a nicety.

## Testing

Everything runs from `uv run python -m tests`. **No network: generation is stubbed.**

| check | what only it catches |
|---|---|
| `mcq.demo()` | a second run generates nothing already in the file; a malformed generation (3 options, `correct` out of range, two options identical after normalisation) is refused and counted rather than written; a 402 partway through keeps what was already generated |
| `evaluate.demo()` | `grade_choice` scores 100/0 and rejects an out-of-range choice |
| `store.demo()` | seeding `mcq.json` is idempotent; an MCQ answer upserts on re-answer; deleting a source question cascades to its MCQ rows; the guarded migration is a no-op on a database that already has the columns |
| `test_api.py` | the MCQ route is gated by the cookie; one user cannot write another's sitting; a closed sitting rejects; an unknown question is a 404; the response does **not** leak whether the answer was correct |
| `test_frontend.py` | the new ids resolve, and every exit from a live MCQ sitting still routes through `leave()` |
| `test_packaging.py` | the wheel ships `data/mcq.json` |

The "does not leak correctness" assertion is worth its line: it is the one property that
distinguishes end-only feedback from per-question feedback, and it lives entirely in a
response body nothing else inspects.

## Explicitly out of scope

- **Per-question immediate feedback.** Considered and rejected; the report is the one place
  results live, matching Vocal.
- **Timed sittings.** Nobody asked, and a clock changes the abandonment story.
- **Spoken MCQ questions.** `GET /questions/{id}/audio` 404s for MCQ rows, which is correct —
  the build step only speaks the Vocal bank.
- **Regenerating options at request time.** Never. The whole format exists to have no model
  in the request path.
- **A `mcqgen` role in `ENDPOINTS`.** Reuses `judge`; add one only when a second model is
  actually wanted.
