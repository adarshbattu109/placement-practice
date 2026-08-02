"""Entrypoint: `uv run practice` starts the server."""

import logging
import logging.handlers

import truststore
import uvicorn
from dotenv import load_dotenv

from src.constants.filepaths import LOG_DIR, LOG_FILE, ROOT

# Use the OS trust store, so HF calls work behind a TLS-intercepting proxy.
truststore.inject_into_ssl()
load_dotenv(ROOT / ".env.local")  # HF_TOKEN, read by huggingface_hub


def setup_logging(level=logging.INFO):
    """Log to logs/server.log and the console. Safe to call more than once."""
    root = logging.getLogger()
    if any(getattr(h, "_practice", False) for h in root.handlers):
        return LOG_FILE

    LOG_DIR.mkdir(exist_ok=True)
    path = LOG_FILE
    # ponytail: 5 x 2MB rotation, switch to TimedRotating if you want daily files
    handler = logging.handlers.RotatingFileHandler(path, maxBytes=2_000_000, backupCount=5)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)-22s %(message)s"))
    handler._practice = True

    root.setLevel(level)
    root.addHandler(handler)
    root.addHandler(logging.StreamHandler())
    # httpx logs a line per request, which buries ours. Warnings still get through.
    for noisy in ("httpx", "httpx2", "httpcore", "httpcore2"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return path


def run(host: str = "0.0.0.0", port: int = 8000, reload: bool = True, workers: int = 1):
    """Run the FastAPI server to host the practice app."""
    path = setup_logging()
    logging.getLogger("app").info("logging to %s", path)
    uvicorn.run(
        "app.main:app",
        host=host,
        port=port,
        reload=reload,
        workers=workers,
        log_level="info",
        log_config=None,  # keep our handlers; uvicorn's default config would replace them
    )
