"""Every path the app touches, resolved once from the repo root."""

import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent

APP_DIR = ROOT / "app"
INDEX_HTML = APP_DIR / "index.html"

DATA_DIR = ROOT / "data"
QUESTIONS_JSON = DATA_DIR / "questions.json"
AUDIO_DIR = DATA_DIR / "audio"  # spoken questions, generated once at build time
DB_PATH = DATA_DIR / "practice.db"

LOG_DIR = ROOT / "logs"
LOG_FILE = LOG_DIR / "server.log"

SCHEMA_SQL = ROOT / "src" / "db" / "schema.sql"


def spoken(question_id, voice):
    """Where one question's generated speech lives: data/audio/<voice>/<id>.opus.

    A directory per voice rather than a flat data/audio/<id>.opus, so a second persona is
    new files instead of renaming every existing one and changing the url with it.
    """
    return AUDIO_DIR / voice / f"{question_id}.opus"
