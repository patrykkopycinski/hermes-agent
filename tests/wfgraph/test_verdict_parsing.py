"""A verdict comes from the verdict line, not from prose.

The prompt in `build_prompt` asks the model to "end with a line that is exactly
PASS or FAIL". `parse_result` did not read it that way: it uppercased the whole
reply and took the LAST \\b(PASS|FAIL)\\b anywhere in it. Ordinary English then
became a gate decision --

    "I could not get it to pass."   -> PASS
    "This should not fail."         -> FAIL

and because the match is last-wins, a closing sentence silently overrode the
real verdict line above it. End to end (probe): an agent replying

    FAIL: the migration is broken.
    I could not get it to pass.

produced verdict PASS, routed down the gate's *ship* arm, and the run finished
`succeeded`. A check that says FAIL must never ship.

The rule these tests pin: read the verdict from a line that IS the verdict
(optionally decorated with markdown/punctuation), scanning bottom-up. Prose that
merely contains the word is not a verdict. A JSON `verdict` field still wins,
since that is unambiguous.
"""

from __future__ import annotations

import pytest

from wfgraph.agent import parse_result


def verdict_of(text: str):
    return parse_result(text)["verdict"]


# --- the bug: prose must not become a verdict -------------------------------


@pytest.mark.parametrize(
    "reply",
    [
        "All the tests pass and nothing seems broken.",
        "I will pass this to the next reviewer.",
        "This should not fail in production.",
        "The COMPASS module is fine.",
        "I looked at it. Seems fine.",
    ],
)
def test_prose_that_merely_mentions_the_words_is_not_a_verdict(reply):
    assert verdict_of(reply) is None


def test_a_closing_sentence_does_not_override_the_real_verdict():
    """The regression that shipped a failing check.

    The agent states FAIL on its own line, then writes a sentence containing
    the word "pass". Last-match-wins read that sentence as the verdict.
    """
    reply = "FAIL: the migration is broken.\nI could not get it to pass."
    assert verdict_of(reply) == "FAIL"


def test_the_inverse_direction_is_also_wrong():
    """A passing check must not be flipped to FAIL by a closing sentence."""
    reply = "PASS: works.\nNothing here can fail."
    assert verdict_of(reply) == "PASS"


# --- guard rails: the shapes the prompt actually asks for -------------------


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ("Checked the config.\n\nPASS", "PASS"),
        ("Found a bug.\n\nFAIL", "FAIL"),
        ("Result: **FAIL**", "FAIL"),
        ("Result: **PASS**", "PASS"),
        ("PASS.", "PASS"),
        ("FAIL!", "FAIL"),
        ("pass", "PASS"),
    ],
)
def test_a_verdict_line_is_still_read(reply, expected):
    assert verdict_of(reply) == expected


def test_the_last_verdict_line_wins_when_there_are_several():
    """Bottom-up: a later verdict line supersedes an earlier one."""
    reply = "First pass:\nFAIL\n\nAfter the fix:\nPASS"
    assert verdict_of(reply) == "PASS"


def test_an_explicit_json_verdict_still_wins():
    reply = '{"verdict": "FAIL", "why": "schema drift"}\n\nEverything else looks fine.'
    result = parse_result(reply)
    assert result["verdict"] == "FAIL"
    assert result["output"]["why"] == "schema drift"


def test_json_verdict_beats_a_contradicting_prose_line():
    reply = '{"verdict": "FAIL"}\n\nPASS'
    assert verdict_of(reply) == "FAIL"


def test_the_summary_and_raw_text_are_untouched():
    """Only verdict reading changes; the reply itself is still carried."""
    reply = "All the tests pass and nothing seems broken."
    result = parse_result(reply)
    assert result["summary"] == reply
    assert result["output"]["text"] == reply


def test_an_empty_reply_has_no_verdict():
    assert verdict_of("") is None
    assert parse_result("")["summary"] == "done"
