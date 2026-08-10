"""Generate the multiple-choice bank once, to a file. Run with `python -m src.helpers.mcq`.

Three questions per source question — one per level — written to `data/mcq.json`, which is
committed. The file rather than the database on purpose: an LLM writes this answer key and it
marks real candidates, so a distractor that is quietly also correct has to be caught in a diff
before it can mark anyone. There is no judge to appeal to at request time.

Re-running skips any source that already has all three, so a topic added later costs only its
own generations. Nothing calls this at request time.
"""

import json
import logging

from src.constants.constants import JUDGE_MAX_TOKENS, LEVELS
from src.constants.filepaths import MCQ_JSON, QUESTIONS_JSON
from src.helpers.inference import chat

log = logging.getLogger("app.mcq")

OPTIONS = 4

SYSTEM = """You write multiple-choice questions for a technical interview trainer.

You receive a source question, its ideal answer, and a target expertise level. Write ONE
multiple-choice question testing the same concept, pitched at that level.

- beginner: the core idea. Distractors are plainly wrong to someone who understands it.
- advanced: the mechanism and its tradeoffs. Distractors are plausible but wrong.
- expert: edge cases, failure modes, precise claims. Distractors are subtly wrong.

Exactly 4 options. EXACTLY ONE is correct — the other three must be unambiguously wrong,
not merely worse. Never write "all of the above" or "none of the above". Options must be
mutually distinct in substance, not near-duplicates differing by a word.

Reply with JSON only, no prose, no code fence:
{"text": str, "options": [str, str, str, str], "correct": int, "explanation": str}

"correct" is the 0-based index into "options". "explanation" is one sentence saying why the
correct option is right, addressed to the candidate."""


def generate(only=None, path=None):
    """Generate every missing MCQ. Returns (written, skipped, failed).

    `only` limits it to one source question id, to audition the prompt without paying for the
    whole bank. A failure is counted and logged rather than raised, and the file is rewritten
    after each one: a 402 partway through must not throw away what already succeeded.
    """
    sources = json.loads(QUESTIONS_JSON.read_text())
    if only is not None:
        sources = [q for q in sources if q["id"] == only]
        if not sources:
            raise ValueError(f"Unknown question_id {only!r}")

    bank = json.loads(path.read_text()) if (path := path or MCQ_JSON).exists() else []
    have = {entry["id"] for entry in bank}

    written, skipped, failed = 0, 0, 0
    for source in sources:
        for level in LEVELS:
            entry_id = f"{source['id']}-{level}"
            if entry_id in have:
                skipped += 1
                continue
            try:
                entry = _one(source, level, entry_id)
            except Exception as e:  # noqa: BLE001 — a bad generation must not lose earlier work
                failed += 1
                log.error("mcq failed for %s: %s", entry_id, e)
                continue
            bank.append(entry)
            # Written per entry, not at the end: the run is 63 remote calls and any of them
            # can be the one that fails.
            path.write_text(json.dumps(bank, indent=2) + "\n")
            written += 1
            log.info("wrote %s", entry_id)
    return written, skipped, failed


def _one(source, level, entry_id):
    """Ask for one question and return the validated entry. Raises if it cannot be trusted."""
    prompt = (
        f"SOURCE QUESTION:\n{source['text']}\n\n"
        f"IDEAL ANSWER:\n{source['ideal_answer']}\n\n"
        f"TARGET LEVEL: {level}"
    )
    raw = json.loads(
        chat(
            "judge",
            [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
            max_tokens=JUDGE_MAX_TOKENS,
            response_format={"type": "json_object"},
        )
    )
    return {
        "id": entry_id,
        "source_id": source["id"],
        "topic": source["topic"],
        "level": level,
        **_validate(raw, source),
    }


def _validate(raw, source):
    """The generated fields, or ValueError. Everything here marks somebody, so none of it is
    taken on trust."""
    text = str(raw.get("text") or "").strip()
    if not text:
        raise ValueError("empty question text")
    if text.casefold() == source["text"].casefold():
        raise ValueError("question is a copy of the source")

    options = raw.get("options")
    if not isinstance(options, list) or len(options) != OPTIONS:
        raise ValueError(f"expected {OPTIONS} options, got {options!r}")
    options = [str(o).strip() for o in options]
    if not all(options):
        raise ValueError("an option is empty")
    # Two options differing only by case or spacing make the question unanswerable, and it is
    # exactly what a fluent model produces when it runs out of distractors. Whitespace is
    # removed rather than collapsed, so "O(1)" and "o( 1 )" compare equal.
    if len({"".join(o.casefold().split()) for o in options}) != OPTIONS:
        raise ValueError(f"options are not distinct: {options}")

    correct = raw.get("correct")
    # bool is an int in Python, and `True` would silently index option 1.
    if isinstance(correct, bool) or not isinstance(correct, int) or not 0 <= correct < OPTIONS:
        raise ValueError(f"correct {correct!r} is not one of 0-{OPTIONS - 1}")

    explanation = str(raw.get("explanation") or "").strip()
    if not explanation:
        raise ValueError("empty explanation")
    return {"text": text, "options": options, "correct": correct, "explanation": explanation}


def demo():
    """Self-check: the skip, the validation, and a mid-run failure. The model is stubbed."""
    import pathlib
    import tempfile

    good = {
        "text": "What is the average-case cost of a hash map lookup?",
        "options": ["O(1)", "O(log n)", "O(n)", "O(n log n)"],
        "correct": 0,
        "explanation": "Hashing finds the bucket directly.",
    }
    sources = json.loads(QUESTIONS_JSON.read_text())
    first = sources[0]

    for bad, why in (
        ({**good, "options": good["options"][:3]}, "3 options"),
        ({**good, "correct": 4}, "index past the end"),
        ({**good, "correct": True}, "a bool indexing as 1"),
        ({**good, "options": ["O(1)", "o( 1 )", "O(n)", "O(n log n)"]}, "duplicate by case"),
        ({**good, "explanation": "  "}, "blank explanation"),
        ({**good, "text": first["text"]}, "copy of the source"),
    ):
        try:
            _validate(bad, first)
            raise AssertionError(f"should have rejected {why}")
        except ValueError:
            pass
    assert _validate(good, first)["correct"] == 0

    real_chat = globals()["chat"]
    with tempfile.TemporaryDirectory() as tmp:
        out = pathlib.Path(tmp) / "mcq.json"
        asked = []
        globals()["chat"] = lambda role, messages, **kw: (
            asked.append(messages[1]["content"]),
            json.dumps(good),
        )[1]
        try:
            assert generate(only=first["id"], path=out) == (len(LEVELS), 0, 0)
            bank = json.loads(out.read_text())
            assert [e["id"] for e in bank] == [f"{first['id']}-{lvl}" for lvl in LEVELS], bank
            assert {e["level"] for e in bank} == set(LEVELS), "one per level, not three the same"
            assert all(e["source_id"] == first["id"] for e in bank)
            assert "TARGET LEVEL: beginner" in asked[0], asked[0]

            # The whole point: a second run pays for nothing it already has.
            asked.clear()
            assert generate(only=first["id"], path=out) == (0, len(LEVELS), 0)
            assert asked == [], "re-generated a question already on disk"

            # A failure is counted, and what was already generated stays on disk.
            globals()["chat"] = lambda role, messages, **kw: (_ for _ in ()).throw(
                RuntimeError("402 payment required")
            )
            log.setLevel(logging.CRITICAL)
            try:
                written, skipped, failed = generate(only=sources[1]["id"], path=out)
            finally:
                log.setLevel(logging.NOTSET)
            assert (written, skipped, failed) == (0, 0, len(LEVELS)), (written, skipped, failed)
            assert json.loads(out.read_text()) == bank, "a failed run lost earlier work"
        finally:
            globals()["chat"] = real_chat

    print(f"ok — {len(LEVELS)} levels per source, skips what exists, refuses 6 bad generations")


def main():
    """CLI: `python -m src.helpers.mcq [question_id]`."""
    import sys

    import truststore
    from dotenv import load_dotenv

    from src.constants.filepaths import ROOT

    # As in speech.py: nothing under src/ may import from app/, but this build step needs the
    # token and the OS trust store just the same.
    truststore.inject_into_ssl()
    load_dotenv(ROOT / ".env.local")
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    written, skipped, failed = generate(sys.argv[1] if len(sys.argv) > 1 else None)
    print(f"{written} written, {skipped} already there, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
