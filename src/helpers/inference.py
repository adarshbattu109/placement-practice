"""Every remote model call in the app, and the one place that decides who serves it.

Two roles talk to models — the judge (chat) and ASR (transcription) — and each can point at
a different provider. `ENDPOINTS` holds the defaults and the environment overrides them, so
switching the judge to Ollama or ASR to Groq is two variables and no code change.

There are only two wire formats to speak. An empty url means the HuggingFace Inference API
and its own client; a url set means an OpenAI-shaped server, which is what Groq, OpenAI,
vLLM, LM Studio and Ollama all serve. Knowing HTTP is the whole job of this module —
nothing above it should ever construct a client or read a model name.
"""

import functools
import logging
import os

from src.constants.constants import ASR_LANGUAGE, ENDPOINTS

log = logging.getLogger("app.inference")


def endpoint(role):
    """Resolve one role's model, url, key and timeout. The environment wins over the table.

    Read at call time, not at import: `.env.local` is loaded by `app/__init__.py`, and this
    module must not care who imports it first. The key falls back to `HF_TOKEN` so pointing
    a role at HuggingFace needs no second variable, and a local server that ignores the key
    needs none at all.
    """
    spec = ENDPOINTS[role]
    name = role.upper()
    return {
        "role": role,
        "model": os.getenv(f"{name}_MODEL") or spec["model"],
        "url": (os.getenv(f"{name}_URL") or spec["url"]).rstrip("/"),
        "key": os.getenv(f"{name}_KEY") or os.getenv("HF_TOKEN") or "",
        "timeout": float(os.getenv(f"{name}_TIMEOUT") or spec["timeout"]),
    }


@functools.cache
def _hub(timeout):
    """The HuggingFace client. Cached per timeout, since roles set their own."""
    from huggingface_hub import InferenceClient

    if not os.getenv("HF_TOKEN"):
        raise RuntimeError("HF_TOKEN is not set. Add it to .env.local, then restart the server.")
    return InferenceClient(token=os.environ["HF_TOKEN"], timeout=timeout)


@functools.cache
def _openai(url, key, timeout):
    """A client for any OpenAI-shaped server. Cached, because it holds a connection pool."""
    import httpx

    return httpx.Client(base_url=url, headers={"Authorization": f"Bearer {key}"}, timeout=timeout)


def reset():
    """Drop the cached clients so the next call re-reads the environment. For tests."""
    _hub.cache_clear()
    _openai.cache_clear()


def chat(role, messages, **options):
    """Send a chat completion and return the reply text.

    Both backends speak chat-completions, so this is one call either way — the HF client
    accepts a `base_url`, which is how it reaches a server that is not HuggingFace.
    """
    at = endpoint(role)
    from huggingface_hub import InferenceClient

    client = (
        # Local servers ignore the key but the client insists on one.
        InferenceClient(base_url=at["url"], api_key=at["key"] or "unused", timeout=at["timeout"])
        if at["url"]
        else _hub(at["timeout"])
    )
    reply = client.chat_completion(model=at["model"], messages=messages, **options)
    return reply.choices[0].message.content


def transcribe(role, audio):
    """Transcribe `audio` bytes and return the text.

    This is where the two backends genuinely differ: HuggingFace takes the raw bytes on its
    own client, and everyone else takes a multipart upload at `POST /audio/transcriptions`.
    Hence the branch — unlike `chat()`, one call cannot cover both.
    """
    at = endpoint(role)
    if not at["url"]:
        result = _hub(at["timeout"]).automatic_speech_recognition(
            audio,
            model=at["model"],
            extra_body={"language": ASR_LANGUAGE, "task": "transcribe"},
        )
        return result.text.strip()

    reply = _openai(at["url"], at["key"], at["timeout"]).post(
        "/audio/transcriptions",
        files={"file": (_filename(audio), audio)},
        data={"model": at["model"], "language": ASR_LANGUAGE, "response_format": "json"},
    )
    reply.raise_for_status()
    return reply.json()["text"].strip()


def _filename(audio):
    """Name the upload after its container: both providers read the format off the extension.

    Sniffed rather than passed in, because the caller has bytes from two different places —
    the stored Opus take, and the WAV a `curl` submit sends straight through.
    """
    return "take.wav" if audio[:4] == b"RIFF" else "take.ogg"


def demo():
    """Self-check: the routing table, the environment overrides, and both call shapes."""
    saved = {
        k: os.environ.get(k)
        for k in ("ASR_URL", "ASR_MODEL", "ASR_KEY", "ASR_TIMEOUT", "JUDGE_URL", "HF_TOKEN")
    }

    def restore():
        for key, value in saved.items():
            os.environ.pop(key, None)
            if value is not None:
                os.environ[key] = value
        reset()

    try:
        for key in saved:
            os.environ.pop(key, None)
        os.environ["HF_TOKEN"] = "test-only"
        reset()

        # Nothing set: the table's defaults, HuggingFace, and HF_TOKEN as the key.
        at = endpoint("asr")
        assert at["url"] == "" and at["model"] == ENDPOINTS["asr"]["model"], at
        assert at["key"] == "test-only" and at["timeout"] == ENDPOINTS["asr"]["timeout"], at
        assert endpoint("judge")["model"] == ENDPOINTS["judge"]["model"]

        # Every field overridable, and a trailing slash must not become a double slash.
        os.environ |= {
            "ASR_URL": "https://api.groq.com/openai/v1/",
            "ASR_MODEL": "whisper-large-v3-turbo",
            "ASR_KEY": "gsk-test",
            "ASR_TIMEOUT": "30",
        }
        at = endpoint("asr")
        assert at["url"] == "https://api.groq.com/openai/v1", at["url"]
        assert at["model"] == "whisper-large-v3-turbo" and at["key"] == "gsk-test", at
        assert at["timeout"] == 30.0, at
        # A role's key must not leak into another's.
        assert endpoint("judge")["key"] == "test-only", endpoint("judge")

        assert _filename(b"RIFF....WAVE") == "take.wav"
        assert _filename(b"OggS\x00\x02") == "take.ogg"

        # The OpenAI-shaped branch: the take must be posted as multipart to the right path,
        # with the model named in the form rather than the url.
        sent = {}

        class Stub:
            def post(self, path, files, data):
                sent.update(path=path, files=files, data=data)
                return type(
                    "R",
                    (),
                    {"raise_for_status": lambda s: None, "json": lambda s: {"text": " hi "}},
                )()

        real = globals()["_openai"]
        globals()["_openai"] = lambda url, key, timeout: Stub()
        try:
            assert transcribe("asr", b"OggS\x00fake") == "hi", "the text must come back stripped"
        finally:
            globals()["_openai"] = real
        assert sent["path"] == "/audio/transcriptions", sent["path"]
        assert sent["data"]["model"] == "whisper-large-v3-turbo", sent["data"]
        assert sent["data"]["language"] == ASR_LANGUAGE, sent["data"]
        assert sent["files"]["file"][0] == "take.ogg", sent["files"]

        # The HuggingFace branch: raw bytes on the hub client, language pinned, no multipart.
        os.environ.pop("ASR_URL")
        reset()
        seen = {}

        class Hub:
            def automatic_speech_recognition(self, audio, model, extra_body):
                seen.update(audio=audio, model=model, extra_body=extra_body)
                return type("R", (), {"text": " heard "})()

        real_hub = globals()["_hub"]
        globals()["_hub"] = lambda timeout: Hub()
        try:
            assert transcribe("asr", b"OggS\x00fake") == "heard"
        finally:
            globals()["_hub"] = real_hub
        assert seen["audio"] == b"OggS\x00fake" and seen["model"] == "whisper-large-v3-turbo", seen
        assert seen["extra_body"]["language"] == ASR_LANGUAGE, seen
        assert seen["extra_body"]["task"] == "transcribe", seen

        # No token and no url is the one configuration that cannot work, and it must say so
        # rather than fail somewhere inside a client.
        os.environ.pop("HF_TOKEN")
        reset()
        try:
            _hub(60)
            raise AssertionError("a missing HF_TOKEN must be refused")
        except RuntimeError as e:
            assert "HF_TOKEN" in str(e), e
    finally:
        restore()

    print(f"ok — {len(ENDPOINTS)} roles resolve, env overrides win, both call shapes dispatch")


if __name__ == "__main__":
    demo()
