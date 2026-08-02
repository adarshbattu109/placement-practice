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
