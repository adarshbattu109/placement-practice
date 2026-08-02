"""Grade a transcribed answer on Completeness, Correctness and Relevance.

An LLM judge scores the three against the ideal answer. Which judge, and where it is served
from, is `src/helpers/inference.py`'s business — this file owns the prompt and the parser.
"""

import json
import re

from src.constants.constants import (
    JUDGE_MAX_TOKENS,
    JUDGE_TEMPERATURE,
    LEVELS,
    MIN_WORDS,
    PARAMETERS,
)
from src.helpers.inference import chat

SYSTEM = """You are a strict but fair technical interviewer grading a spoken answer.

You receive the interview question, an ideal answer, the candidate's expertise level,
and a transcript of what they actually said. The transcript comes from speech
recognition, so ignore punctuation, capitalisation and obvious homophone slips —
grade the substance.

Grade against the bar for their stated level, not an absolute bar. The same answer
should score higher for a beginner than for an expert.

Score each parameter from 0 to 100:
- completeness: how much of the ideal answer's substance the candidate covered.
- correctness: whether what they said is technically true. Confident wrong claims
  score low here even if they cover the right topics. An answer that is thin but
  entirely accurate scores high.
- relevance: whether they answered the question that was asked, without padding
  or drifting to an adjacent topic.

Reply with JSON only, no prose, no code fence:
{"completeness": {"score": int, "note": str},
 "correctness": {"score": int, "note": str, "errors": [str]},
 "relevance": {"score": int, "note": str},
 "strengths": [str], "gaps": [str], "suggestion": str}

Each note is one sentence. "errors" lists specific incorrect claims, empty if none.
"strengths" and "gaps" hold at most three short items each, addressed to the
candidate as "you". "suggestion" is one concrete sentence on what to do next."""


def _judge(transcript, question, ideal_answer, level):
    """Ask the judge model for the three scores. Returns the parsed dict."""
    prompt = (
        f"QUESTION:\n{question}\n\n"
        f"IDEAL ANSWER:\n{ideal_answer}\n\n"
        f"EXPERTISE LEVEL: {level}\n{LEVELS[level]['brief']}\n\n"
        f"CANDIDATE TRANSCRIPT:\n{transcript}"
    )
    return _parse(
        chat(
            "judge",
            [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
            max_tokens=JUDGE_MAX_TOKENS,
            temperature=JUDGE_TEMPERATURE,
            response_format={"type": "json_object"},
        )
    )


def _parse(raw):
    """Tolerate a code fence or stray prose around the JSON."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            raise ValueError(f"Judge did not return JSON: {raw[:200]}") from None
        data = json.loads(match.group())

    for name in PARAMETERS:
        entry = data.get(name)
        if not isinstance(entry, dict) or "score" not in entry:
            raise ValueError(f"Judge omitted {name!r}: {str(data)[:200]}")
        entry["score"] = max(0, min(100, int(entry["score"])))
        entry.setdefault("note", "")
    return data


def evaluate(transcript, question, ideal_answer, level="advanced"):
    """Grade `transcript` against `ideal_answer` for `question`, at `level`'s bar."""
    if level not in LEVELS:
        raise ValueError(f"Unknown level {level!r}. Expected one of {sorted(LEVELS)}.")

    transcript = (transcript or "").strip()
    if len(transcript.split()) < MIN_WORDS:
        return {
            "transcript": transcript,
            "score": 0,
            "level": level,
            "weights": LEVELS[level]["weights"],
            "parameters": {p: {"score": 0, "note": "Nothing was said."} for p in PARAMETERS},
            "errors": [],
            "comments": {
                "strengths": [],
                "gaps": ["No answer detected in the recording."],
                "suggestion": "Check your microphone, then record again and talk for at least 60 seconds.",
            },
        }

    verdict = _judge(transcript, question, ideal_answer, level)
    parameters = {p: verdict[p] for p in PARAMETERS}
    weights = LEVELS[level]["weights"]
    score = round(sum(weights[p] * parameters[p]["score"] for p in PARAMETERS))

    return {
        "transcript": transcript,
        "score": score,
        "level": level,
        "weights": weights,
        "parameters": parameters,
        "errors": verdict["correctness"].get("errors") or [],
        "comments": {
            "strengths": verdict.get("strengths") or [],
            "gaps": verdict.get("gaps") or [],
            "suggestion": verdict.get("suggestion") or "",
        },
    }


def demo():
    """Self-check: parsing and per-level weighting, with the judge stubbed out.

    Which backend serves the judge is `inference.demo()`'s business now, not this one's.
    """
    global _judge

    assert (
        _parse(
            '```json\n{"completeness":{"score":150},"correctness":{"score":-5},'
            '"relevance":{"score":40}}\n```'
        )["completeness"]["score"]
        == 100
    )
    assert (
        _parse(
            'noise {"completeness":{"score":"70"},"correctness":{"score":80},'
            '"relevance":{"score":90}} tail'
        )["completeness"]["score"]
        == 70
    )
    for bad in ("not json at all", '{"completeness":{"score":50}}'):
        try:
            _parse(bad)
            raise AssertionError(f"should have rejected {bad!r}")
        except ValueError:
            pass

    real = _judge
    _judge = lambda t, q, i, level: {
        "completeness": {"score": 90, "note": "n"},
        "correctness": {"score": 30, "note": "n", "errors": ["said O(n) for average lookup"]},
        "relevance": {"score": 90, "note": "n"},
        "strengths": ["Covered the mechanics."],
        "gaps": ["The complexity claim was wrong."],
        "suggestion": "Recheck average versus worst case.",
    }
    answer = "a hash map hashes the key to a bucket and average lookup is order n"
    try:
        # Same judge scores (comp 90, corr 30, rel 90) — only the weights differ.
        scored = {lvl: evaluate(answer, "Q", "IDEAL", lvl) for lvl in LEVELS}
        blank = evaluate("uh", "Q", "IDEAL", "beginner")
        for bad in ("Beginner", "guru", None):
            try:
                evaluate("a b c d e f", "Q", "IDEAL", bad)
                raise AssertionError(f"should have rejected level {bad!r}")
            except ValueError:
                pass
    finally:
        _judge = real

    # 0.20*90 + 0.55*30 + 0.25*90 = 57 | 0.35/0.40/0.25 = 66 | 0.45/0.35/0.20 = 69
    assert scored["beginner"]["score"] == 57, scored["beginner"]["score"]
    assert scored["advanced"]["score"] == 66, scored["advanced"]["score"]
    assert scored["expert"]["score"] == 69, scored["expert"]["score"]
    # Correctness is weak here, so the correctness-heavy beginner bar must score it lowest.
    assert scored["beginner"]["score"] < scored["advanced"]["score"]

    r = scored["expert"]
    assert r["errors"] == ["said O(n) for average lookup"]
    assert set(r["parameters"]) == set(PARAMETERS)
    assert r["level"] == "expert" and r["weights"] == LEVELS["expert"]["weights"]
    assert blank["score"] == 0 and blank["parameters"]["correctness"]["score"] == 0
    assert blank["weights"] == LEVELS["beginner"]["weights"]
    print(
        "ok — "
        + " ".join(f"{k}={v['score']}" for k, v in scored.items())
        + f" empty={blank['score']}"
    )


if __name__ == "__main__":
    demo()
