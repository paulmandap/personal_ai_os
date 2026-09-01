"""Controls for the ADR-060 bisection's classifier.

Same reasoning as `test_count_mechanisms.py`: the script's *number* is only worth
reading if its labelling has been shown to fire when it should and stay silent
when it should not. `classify` is deliberately pure so this runs with Ollama
stopped and sockets blocked -- the network lives in `ask`, which is not tested
here because it has nothing to decide.

The distinction these pin is the one the ADR turns on: **`TEXT` is not
recovery.** A Master that describes a delegation instead of calling one has not
delegated, so an arm that converts EMPTY into TEXT must not be read as a fix.
"""

from __future__ import annotations

from adr060_bisect_master_prompt import BULLETS, MASTER_SYSTEM_PROMPT, classify


class TestClassify:
    def test_a_parsed_tool_call_is_CALL(self):
        message = {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"function": {"name": "delegate", "arguments": {}}}],
        }
        assert classify(message) == "CALL"

    def test_a_tool_call_wins_over_content(self):
        """The agent loop acts on the call, so a turn that does both is a CALL."""
        message = {
            "content": "I will delegate this.",
            "tool_calls": [{"function": {"name": "delegate", "arguments": {}}}],
        }
        assert classify(message) == "CALL"

    def test_content_without_a_call_is_TEXT(self):
        assert classify({"content": "I will delegate this."}) == "TEXT"

    def test_prose_that_merely_names_the_tool_is_still_TEXT(self):
        """The exact 3B output ADR-060 recorded with no tools advertised.

        It reads like a delegation and is not one. Scoring it as recovery is the
        error this control exists to prevent.
        """
        message = {"content": 'delegate {"task": "renew_passport"}'}
        assert classify(message) == "TEXT"

    def test_no_content_and_no_call_is_EMPTY(self):
        assert classify({"role": "assistant", "content": ""}) == "EMPTY"

    def test_whitespace_only_is_EMPTY(self):
        assert classify({"content": "  \n "}) == "EMPTY"

    def test_a_missing_content_key_is_EMPTY(self):
        """Ollama omits `content` in some payload shapes; absent is not TEXT."""
        assert classify({"role": "assistant"}) == "EMPTY"

    def test_an_empty_tool_calls_list_is_not_a_CALL(self):
        assert classify({"content": "", "tool_calls": []}) == "EMPTY"


class TestArmsAreWellFormed:
    """If the prompt is ever restructured, the bisection must fail loudly."""

    def test_the_bullets_were_actually_found(self):
        assert len(BULLETS) >= 4, BULLETS

    def test_every_bullet_came_from_the_real_prompt(self):
        for bullet in BULLETS:
            assert bullet.strip()[:30] in MASTER_SYSTEM_PROMPT

    def test_the_roster_placeholder_still_exists(self):
        """Every arm substitutes `{roster}`; a rename would silently ship it."""
        assert "{roster}" in MASTER_SYSTEM_PROMPT
