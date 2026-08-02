-- Every table is STRICT so a typo in a type is an error, not a surprise at read time.
-- Run repeatedly: store.setup() applies this on every open, so keep it idempotent.

CREATE TABLE IF NOT EXISTS users (
  id            INTEGER PRIMARY KEY,
  email         TEXT NOT NULL UNIQUE,       -- stored lowercased; the app normalises
  password_hash TEXT NOT NULL,
  created_at    TEXT NOT NULL DEFAULT (datetime('now'))
) STRICT;

-- Split from users so the display name and defaults can change without touching
-- the credential row.
CREATE TABLE IF NOT EXISTS profiles (
  user_id       INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  name          TEXT NOT NULL,
  default_level TEXT NOT NULL DEFAULT 'advanced'
) STRICT;

CREATE TABLE IF NOT EXISTS topics (
  id         INTEGER PRIMARY KEY,
  name       TEXT NOT NULL UNIQUE,
  notes      TEXT,                          -- source material questions were generated from
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
) STRICT;

-- id stays TEXT ('dsa-1'): the browser sends it back on /submit and the spoken
-- question is served from data/audio/<id>.opus.
CREATE TABLE IF NOT EXISTS questions (
  id           TEXT PRIMARY KEY,
  topic_id     INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
  text         TEXT NOT NULL,
  ideal_answer TEXT NOT NULL,
  created_at   TEXT NOT NULL DEFAULT (datetime('now'))
) STRICT;

CREATE INDEX IF NOT EXISTS questions_by_topic ON questions(topic_id);

-- One row per interview sitting. topic is NULL for "every topic", and overall and
-- finished_at stay NULL until the candidate submits, so an abandoned sitting is
-- distinguishable from a completed one rather than looking like a zero.
CREATE TABLE IF NOT EXISTS assessments (
  id          INTEGER PRIMARY KEY,
  user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  topic       TEXT,
  level       TEXT NOT NULL,
  questions   INTEGER,                      -- how many were put to the candidate
  overall     INTEGER,
  started_at  TEXT NOT NULL DEFAULT (datetime('now')),
  finished_at TEXT
) STRICT;

CREATE INDEX IF NOT EXISTS assessments_by_user ON assessments(user_id, started_at DESC);

-- report holds the judge's whole verdict as JSON, not just the scores: the report page
-- needs the notes, strengths, gaps and errors too. score is lifted out of it because it
-- is the one field aggregated in SQL.
CREATE TABLE IF NOT EXISTS answers (
  id            INTEGER PRIMARY KEY,
  assessment_id INTEGER NOT NULL REFERENCES assessments(id) ON DELETE CASCADE,
  question_id   TEXT NOT NULL REFERENCES questions(id),
  transcript    TEXT NOT NULL,
  report        TEXT,
  score         INTEGER,
  seconds       REAL NOT NULL,
  created_at    TEXT NOT NULL DEFAULT (datetime('now')),
  -- Re-recording an answer replaces it. Without this a second take would be graded
  -- twice and averaged in twice.
  UNIQUE (assessment_id, question_id)
) STRICT;

-- The candidate's take, kept so a score can be disputed and so ASR can be re-run without
-- asking them to record it again. A separate table, not a column on answers: a ~200 kB
-- blob on the row the report page reads would drag the audio into every history query,
-- and rewrite the whole overflow chain into the WAL on every re-take.
CREATE TABLE IF NOT EXISTS recordings (
  id            INTEGER PRIMARY KEY,
  assessment_id INTEGER REFERENCES assessments(id) ON DELETE CASCADE,
  question_id   TEXT NOT NULL REFERENCES questions(id),
  audio         BLOB NOT NULL,                -- Opus in Ogg, transcoded from the sent WAV
  mime          TEXT NOT NULL DEFAULT 'audio/ogg',
  seconds       REAL NOT NULL,
  created_at    TEXT NOT NULL DEFAULT (datetime('now')),
  -- Mirrors answers: a re-take replaces the take it will be graded from. assessment_id is
  -- NULL for a one-off curl submit, and SQLite treats NULLs as distinct, so those
  -- accumulate rather than upsert. They are pruned on age like everything else.
  UNIQUE (assessment_id, question_id)
) STRICT;

CREATE INDEX IF NOT EXISTS recordings_by_age ON recordings(created_at);

-- One row per take, not one per stage: `kind` advances from 'transcribe' to 'grade' as the
-- worker gets through it. Keeping it to one row means UNIQUE (assessment_id, question_id)
-- can mirror answers and recordings, so a re-submit of an open sitting resets the work
-- instead of racing an older job for the same question. attempts resets at each stage — a
-- flaky ASR call must
-- not spend the judge's retries.
-- status: pending -> running -> done, or failed once attempts run out.
CREATE TABLE IF NOT EXISTS jobs (
  id            INTEGER PRIMARY KEY,
  assessment_id INTEGER NOT NULL REFERENCES assessments(id) ON DELETE CASCADE,
  question_id   TEXT NOT NULL REFERENCES questions(id),
  kind          TEXT NOT NULL DEFAULT 'transcribe',
  status        TEXT NOT NULL DEFAULT 'pending',
  attempts      INTEGER NOT NULL DEFAULT 0,
  error         TEXT,                         -- the last failure, kept so a report can say why
  claimed_at    TEXT,                         -- when a worker took it; NULL when it is waiting
  created_at    TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (assessment_id, question_id)
) STRICT;

-- The claim query orders by id within status, and the status page groups by assessment.
CREATE INDEX IF NOT EXISTS jobs_waiting ON jobs(status, id);
CREATE INDEX IF NOT EXISTS jobs_by_assessment ON jobs(assessment_id);
