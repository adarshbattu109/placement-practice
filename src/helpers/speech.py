"""Speak the question bank once, to disk. Run it with `python -m src.helpers.speech`.

Every candidate hears the same sentences, so generating on demand would pay ~70s of latency
and a fresh charge for byte-identical audio. This walks the bank, skips anything already on
disk, and writes what is missing — so it is safe to re-run, and re-running is how a topic
added later gets its voice.

Nothing calls this at request time. `app/main.py` serves the files it leaves behind.
"""

import io
import logging

import soundfile as sf

from src.constants.constants import TTS_OPUS_RATE, TTS_VOICE_DEFAULT, TTS_VOICES
from src.constants.filepaths import spoken
from src.db import store
from src.helpers.inference import speak

log = logging.getLogger("app.speech")


def generate(voice=TTS_VOICE_DEFAULT, only=None, path=None):
    """Generate every missing question in `voice`. Returns (written, skipped, failed).

    `only` limits it to one question id, which is how you audition a new voice without
    paying for the whole bank. A failure is counted and logged rather than raised: one 502
    two questions in must not throw away the twenty that already succeeded.
    """
    if voice not in TTS_VOICES:
        raise ValueError(f"Unknown voice {voice!r}. Expected one of {sorted(TTS_VOICES)}.")

    bank = [q for q in store.questions(path=path) if only is None or q["id"] == only]
    if only and not bank:
        raise ValueError(f"Unknown question_id {only!r}")

    written, skipped, failed = 0, 0, 0
    for question in bank:
        target = spoken(question["id"], voice)
        if target.exists():
            skipped += 1
            continue
        try:
            audio = speak("tts", question["text"], voice=voice)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(_opus(audio))
        except Exception as e:  # noqa: BLE001 — a remote failure must not lose earlier work
            failed += 1
            log.error("tts failed for %s: %s", question["id"], e)
        else:
            written += 1
            log.info("spoke %s as %s — %dkB", question["id"], voice, target.stat().st_size // 1024)
    return written, skipped, failed


def _opus(audio):
    """Re-encode whatever the model returned as Opus in Ogg.

    Kokoro answers with a 24 kHz WAV, which is ~12x the size of the Opus for the same audio
    (measured: 226 kB → 19.3 kB) — and this is committed to the repository, so the size is
    the whole point. Resampled only if the model ever returns a rate Opus refuses: libsndfile
    accepts 8/12/16/24/48 kHz and nothing else.
    """
    samples, rate = sf.read(io.BytesIO(audio), dtype="float32", always_2d=True)
    mono = samples.mean(axis=1)
    if rate not in (8000, 12000, 16000, 24000, 48000):
        raise ValueError(f"{rate} Hz cannot be encoded as Opus; expected TTS_OPUS_RATE")

    out = io.BytesIO()
    sf.write(out, mono, rate, format="OGG", subtype="OPUS")
    return out.getvalue()


def demo():
    """Self-check: the skip, the re-encode, and an unknown voice. The model is stubbed."""
    import numpy as np

    from src.constants import filepaths

    # A 24 kHz WAV is what Kokoro returns, so that is what the stub returns.
    tone = np.sin(np.linspace(0, 220 * 2 * np.pi, TTS_OPUS_RATE)).astype("float32")
    wav = io.BytesIO()
    sf.write(wav, tone, TTS_OPUS_RATE, format="WAV", subtype="PCM_16")
    fake = wav.getvalue()

    opus = _opus(fake)
    assert opus[:4] == b"OggS", "the re-encode must produce an Ogg container"
    assert len(opus) < len(fake) / 4, (len(fake), len(opus))  # measured ~12x; 4x is the floor
    frames, rate = sf.read(io.BytesIO(opus), dtype="float32")
    assert rate == TTS_OPUS_RATE and len(frames) > 0, (rate, len(frames))

    # 44.1 kHz is the rate a different provider would plausibly return, and Opus refuses it.
    odd = io.BytesIO()
    sf.write(odd, tone, 44100, format="WAV", subtype="PCM_16")
    try:
        _opus(odd.getvalue())
        raise AssertionError("a rate Opus cannot encode must be refused")
    except ValueError as e:
        assert "44100" in str(e), e

    import pathlib
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        db = pathlib.Path(tmp) / "s.db"
        store.setup(db)
        real_audio, real_speak = filepaths.AUDIO_DIR, globals()["speak"]
        filepaths.AUDIO_DIR = pathlib.Path(tmp) / "audio"
        asked = []
        globals()["speak"] = lambda role, text, voice=None: (asked.append((text, voice)), fake)[1]
        try:
            bank = store.questions(path=db)
            written, skipped, failed = generate(only=bank[0]["id"], path=db)
            assert (written, skipped, failed) == (1, 0, 0), (written, skipped, failed)
            assert asked == [(bank[0]["text"], TTS_VOICE_DEFAULT)], asked
            # The path carries the voice, so a second persona is new files, not a rename.
            assert spoken(bank[0]["id"], TTS_VOICE_DEFAULT).exists()
            assert spoken(bank[0]["id"], TTS_VOICE_DEFAULT).parent.name == TTS_VOICE_DEFAULT

            # The whole point of the slice: a second run generates nothing it already has.
            asked.clear()
            written, skipped, failed = generate(path=db)
            assert (written, skipped) == (len(bank) - 1, 1), (written, skipped, len(bank))
            assert (bank[0]["text"], TTS_VOICE_DEFAULT) not in asked, "re-spoke a question"
            assert generate(path=db) == (0, len(bank), 0), "a third run must do nothing at all"

            # A remote failure is counted, not raised: the rest of the bank must still land.
            globals()["speak"] = lambda role, text, voice=None: (_ for _ in ()).throw(
                RuntimeError("402 payment required")
            )
            spoken(bank[0]["id"], TTS_VOICE_DEFAULT).unlink()
            log.setLevel(logging.CRITICAL)
            try:
                assert generate(path=db) == (0, len(bank) - 1, 1), "one failure, the rest skipped"
            finally:
                log.setLevel(logging.NOTSET)
        finally:
            filepaths.AUDIO_DIR, globals()["speak"] = real_audio, real_speak

    try:
        generate(voice="bm_nonexistent")
        raise AssertionError("an unlisted voice must be refused before it costs a call")
    except ValueError as e:
        assert "bm_nonexistent" in str(e), e

    print(f"ok — bank speaks once and skips thereafter, {len(opus)}B opus from {len(fake)}B wav")


def main():
    """CLI: `python -m src.helpers.speech [voice] [question_id]`."""
    import sys

    import truststore
    from dotenv import load_dotenv

    from src.constants.filepaths import ROOT

    # What app/__init__.py does at import, done here instead: nothing under src/ may import
    # from app/, and this build step needs the token and the OS trust store just as much.
    truststore.inject_into_ssl()
    load_dotenv(ROOT / ".env.local")
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    voice = sys.argv[1] if len(sys.argv) > 1 else TTS_VOICE_DEFAULT
    only = sys.argv[2] if len(sys.argv) > 2 else None
    written, skipped, failed = generate(voice, only)
    print(f"{voice}: {written} written, {skipped} already there, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
