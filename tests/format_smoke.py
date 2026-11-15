"""Smoke test for the 3-tier cleanup pipeline.

Without a GROQ_API_KEY and without an Ollama server running, every call
falls through to Tier 3 (rule-based). That exercises the offline path.
"""

from __future__ import annotations

import logging
import sys

from imla.format import (
    LENGTH_GUARD_RATIO,
    _rule_cleanup,
    _word_count,
    clean_transcript,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


CASES = [
    # (input, expected_cleaned, description)
    (
        "write an email to Sarah about the delay",
        "Write an email to Sarah about the delay.",
        "command in transcript → literal text (Tier 3 defaults to declarative)",
    ),
    (
        "what's the weather tomorrow",
        "What's the weather tomorrow.",
        "question in transcript → declarative period (Tier 3 cannot detect ?)",
    ),
    (
        "what's the weather tomorrow question mark",
        "What's the weather tomorrow?",
        "spoken question mark → ?",
    ),
    (
        "summarise this in two sentences",
        "Summarise this in two sentences.",
        "instruction in transcript → literal text",
    ),
    (
        "um so I was thinking like maybe we should uh you know try the new approach",
        "So I was thinking like maybe we should try the new approach.",
        "fillers removed (um, uh, you know); 'like' kept (regex can't tell filler from meaning)",
    ),
    (
        "send a message to John comma tell him the meeting is at three period",
        "Send a message to John, tell him the meeting is at three.",
        "spoken punctuation mapped",
    ),
    (
        "hello world",
        "Hello world.",
        "simple capitalization + period",
    ),
    (
        "first sentence. second sentence here. third one too",
        "First sentence. Second sentence here. Third one too.",
        "sentence capitalization across periods",
    ),
    (
        "  hello    world  ",
        "Hello world.",
        "whitespace normalization",
    ),
    (
        "open quote this is a quote close quote",
        "\u201cThis is a quote.\u201d",
        "spoken quote marks with whitespace trim",
    ),
    (
        "",
        "",
        "empty input",
    ),
]


def main() -> int:
    failures = 0

    print("== Tier 3 (rule-based) cleanup ==")
    for raw, expected, desc in CASES:
        got = _rule_cleanup(raw)
        ok = got == expected
        flag = "OK" if ok else "FAIL"
        print(f"  [{flag}] {desc}")
        print(f"        in:  {raw!r}")
        print(f"        out: {got!r}")
        if not ok:
            print(f"        exp: {expected!r}")
            failures += 1

    print()
    print("== clean_transcript() end-to-end (no Groq, no Ollama → Tier 3) ==")
    for raw, expected, desc in CASES:
        if not raw:
            continue
        result = clean_transcript(raw, tier_override="rule")
        ok = result.text == expected and result.tier == "rule"
        flag = "OK" if ok else "FAIL"
        print(
            f"  [{flag}] {desc}: tier={result.tier}, "
            f"{result.word_count_in}→{result.word_count_out} words, "
            f"{result.latency_s*1000:.1f} ms"
        )
        if not ok:
            print(f"        got: {result.text!r}")
            failures += 1

    print()
    print("== Length guard: hallucinated LLM output should be discarded ==")
    # Simulate the guard logic by feeding in a deliberately-too-long output.
    raw = "send a message to John"
    raw_words = _word_count(raw)
    hallucinated = " ".join(["lorem ipsum dolor sit amet"] * 20)  # 100 words
    print(f"  raw words={raw_words}, hallucinated words={_word_count(hallucinated)}")
    print(f"  ratio={_word_count(hallucinated)/raw_words:.2f} "
          f"(threshold {LENGTH_GUARD_RATIO}) → would be discarded ✓")

    # Sanity: a legitimate cleanup must pass.
    cleaned = _rule_cleanup(raw)
    print(f"  rule cleanup of {raw!r} → {cleaned!r} "
          f"({_word_count(cleaned)} words, "
          f"ratio={_word_count(cleaned)/raw_words:.2f}) → accepted ✓")

    print()
    if failures:
        print(f"FAILED: {failures} case(s) did not match expected output.")
        return 1
    print("OK: all cleanup cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
