"""Controls for the ADR-060 bisection.

Same reasoning as `test_count_mechanisms.py`: the script's numbers are only
worth reading if its labelling and its arm construction have been shown to do
what they claim. Everything here is pure -- `classify` and `build` take no
network, so this runs with Ollama stopped and sockets blocked. The HTTP lives in
`ask`, which has nothing to decide.

Two failure classes these exist to prevent:

**A silently-changed prompt measured as if it were the shipped one.** If
`MASTER_SYSTEM_PROMPT` is edited, the equality test below fails loudly instead
of the bisection quietly measuring a different string.

**TEXT read as recovery.** A Master that describes a delegation has not
delegated, so an arm converting EMPTY into TEXT must never count as a fix.
"""

from __future__ import annotations

import re

from adr060_bisect_master_prompt import (
    AGENTS_4,
    BULLETS,
    FILLER,
    MASTER_SYSTEM_PROMPT,
    RESTORED_MIN,
    ROSTER_1,
    ROSTER_3,
    ROSTER_4,
    SUPPRESSED_MAX,
    build,
    classify,
    control_arms,
    stage1_arms,
    summarise,
)


class TestArmConstruction:
    """Deterministic construction, pinned so it cannot drift after the fact."""

    def test_the_prompt_still_has_five_bullets(self):
        assert len(BULLETS) == 5, BULLETS

    def test_every_bullet_came_from_the_real_prompt(self):
        for bullet in BULLETS:
            assert bullet in MASTER_SYSTEM_PROMPT

    def test_the_roster_placeholder_still_exists(self):
        assert "{roster}" in MASTER_SYSTEM_PROMPT

    def test_FULL_reproduces_the_shipped_prompt_byte_for_byte(self):
        """The load-bearing invariant.

        If this fails, `FULL` is no longer the shipped prompt and every other
        arm is being compared against something the system never sends.
        """
        assert build(BULLETS, ROSTER_4) == MASTER_SYSTEM_PROMPT.replace(
            "{roster}", ROSTER_4
        )

    def test_dropping_a_bullet_removes_only_that_bullet(self):
        kept = [b for j, b in enumerate(BULLETS) if j != 2]
        arm = build(kept, ROSTER_4)
        assert BULLETS[2] not in arm
        for bullet in kept:
            assert bullet in arm

    def test_an_empty_bullet_list_drops_the_header_too(self):
        """A dangling "How to work:" with nothing under it is an artifact.

        No shipped configuration produces that string, so leaving it in would be
        a confound rather than a control.
        """
        head_only = build([], ROSTER_4)
        assert "How to work:" not in head_only
        assert ROSTER_4 in head_only

    def test_roster_arms_swap_only_the_roster(self):
        assert "research" not in build(BULLETS, ROSTER_3)
        assert "finance" in build(BULLETS, ROSTER_3)
        assert "finance" not in build(BULLETS, ROSTER_1)
        for bullet in BULLETS:
            assert bullet in build(BULLETS, ROSTER_1)

    def test_filler_carries_no_tool_or_turn_instruction(self):
        """`STRUCT_4` is a bullet-count control; inert content is the point.

        Whole words only. A substring match flags "automati*call*y", which is
        not an instruction about anything -- and a control that fails on its own
        false positive teaches you to loosen it, which is how a real one later
        gets waved through.
        """
        banned = ("tool", "tools", "delegate", "agent", "reply", "call", "turn")
        for bullet in FILLER:
            words = set(re.findall(r"[a-z]+", bullet.lower()))
            assert words.isdisjoint(banned), (words & set(banned), bullet)


class TestArmTable:
    def test_stage1_has_seventeen_distinct_arms(self):
        arms = stage1_arms()
        assert len(arms) == 17
        assert len({a.name for a in arms}) == 17

    def test_exactly_one_arm_is_unseeded(self):
        unseeded = [a.name for a in stage1_arms() if not a.seeded]
        assert unseeded == ["FULL_UNSEEDED"]

    def test_the_seeded_and_unseeded_full_arms_share_a_prompt(self):
        """They must differ ONLY in whether `seed` is sent."""
        by_name = {a.name: a for a in stage1_arms()}
        assert by_name["FULL"].system == by_name["FULL_UNSEEDED"].system

    def test_trivial_advertises_no_roster_so_validity_is_not_applicable(self):
        by_name = {a.name: a for a in stage1_arms()}
        assert by_name["TRIVIAL"].agents is None

    def test_the_control_block_leads_with_the_shipped_prompt(self):
        assert control_arms()[0].name == "FULL"
        assert control_arms()[0].system == build(BULLETS, ROSTER_4)


class TestClassify:
    CALL = {"function": {"name": "delegate", "arguments": {"agent": "task_agent", "objective": "x"}}}

    def test_a_parsed_tool_call_is_CALL(self):
        out = classify({"content": "", "tool_calls": [self.CALL]}, AGENTS_4)
        assert out["label"] == "CALL"
        assert out["call_any"] is True
        assert out["valid_delegation"] is True

    def test_a_tool_call_wins_over_content(self):
        out = classify({"content": "I will delegate.", "tool_calls": [self.CALL]}, AGENTS_4)
        assert out["label"] == "CALL"

    def test_content_without_a_call_is_TEXT(self):
        out = classify({"content": "I will delegate this."}, AGENTS_4)
        assert out["label"] == "TEXT"
        assert out["call_any"] is False

    def test_prose_that_merely_names_the_tool_is_still_TEXT(self):
        """The exact 3B output ADR-060 recorded with no tools advertised."""
        out = classify({"content": 'delegate {"task": "renew_passport"}'}, AGENTS_4)
        assert out["label"] == "TEXT"

    def test_no_content_and_no_call_is_EMPTY(self):
        assert classify({"content": ""}, AGENTS_4)["label"] == "EMPTY"

    def test_whitespace_only_is_EMPTY(self):
        assert classify({"content": "  \n "}, AGENTS_4)["label"] == "EMPTY"

    def test_a_missing_content_key_is_EMPTY(self):
        assert classify({"role": "assistant"}, AGENTS_4)["label"] == "EMPTY"

    def test_an_empty_tool_calls_list_is_not_a_CALL(self):
        assert classify({"content": "", "tool_calls": []}, AGENTS_4)["label"] == "EMPTY"


class TestValidDelegation:
    """CALL_ANY and VALID_DELEGATION must come apart -- that is why both exist."""

    def test_an_invented_agent_is_a_CALL_but_not_a_VALID_DELEGATION(self):
        """ADR-060 saw the 3B emit exactly this: `passportRenewalAgent`."""
        call = {
            "function": {
                "name": "delegate",
                "arguments": {"agent": "passportRenewalAgent", "objective": "x"},
            }
        }
        out = classify({"content": "", "tool_calls": [call]}, AGENTS_4)
        assert out["call_any"] is True
        assert out["valid_delegation"] is False
        assert out["label"] == "CALL"

    def test_an_empty_objective_is_not_a_VALID_DELEGATION(self):
        call = {
            "function": {
                "name": "delegate",
                "arguments": {"agent": "task_agent", "objective": "   "},
            }
        }
        out = classify({"content": "", "tool_calls": [call]}, AGENTS_4)
        assert out["call_any"] is True
        assert out["valid_delegation"] is False

    def test_arguments_may_arrive_as_a_json_string(self):
        call = {
            "function": {
                "name": "delegate",
                "arguments": '{"agent": "task_agent", "objective": "x"}',
            }
        }
        assert classify({"tool_calls": [call]}, AGENTS_4)["valid_delegation"] is True

    def test_malformed_argument_json_is_a_CALL_but_not_valid(self):
        call = {"function": {"name": "delegate", "arguments": "{not json"}}
        out = classify({"tool_calls": [call]}, AGENTS_4)
        assert out["call_any"] is True
        assert out["valid_delegation"] is False

    def test_an_agent_outside_this_arms_roster_is_not_valid(self):
        """`ROSTER_1` advertises only task_agent, so naming finance is invalid."""
        call = {
            "function": {
                "name": "delegate",
                "arguments": {"agent": "finance", "objective": "x"},
            }
        }
        assert classify({"tool_calls": [call]}, frozenset({"task_agent"}))[
            "valid_delegation"
        ] is False

    def test_validity_is_not_applicable_when_no_roster_is_advertised(self):
        out = classify({"tool_calls": [self_call()]}, None)
        assert out["call_any"] is True
        assert out["valid_delegation"] is None


def self_call() -> dict:
    return {
        "function": {
            "name": "delegate",
            "arguments": {"agent": "task_agent", "objective": "x"},
        }
    }


class TestSummarise:
    @staticmethod
    def _obs(label: str, n: int) -> list[dict]:
        return [
            {
                "call_any": label == "CALL",
                "valid_delegation": label == "CALL",
                "label": label,
                "prompt_eval_count": 500,
            }
            for _ in range(n)
        ]

    def test_all_calls_is_RESTORED(self):
        assert summarise(self._obs("CALL", 15))["label"] == "RESTORED"

    def test_all_empty_is_SUPPRESSED(self):
        assert summarise(self._obs("EMPTY", 15))["label"] == "SUPPRESSED"

    def test_all_text_is_SUPPRESSED_because_TEXT_is_not_recovery(self):
        """15/15 TEXT means the model never called the tool. Not a fix."""
        summary = summarise(self._obs("TEXT", 15))
        assert summary["call_any"] == 0
        assert summary["label"] == "SUPPRESSED"

    def test_the_middle_band_is_MIXED_and_licenses_no_claim(self):
        for calls in range(SUPPRESSED_MAX + 1, RESTORED_MIN):
            obs = self._obs("CALL", calls) + self._obs("EMPTY", 15 - calls)
            assert summarise(obs)["label"] == "MIXED", calls

    def test_the_thresholds_leave_a_deliberate_gap(self):
        assert SUPPRESSED_MAX + 1 < RESTORED_MIN

    def test_prompt_tokens_are_recorded_so_length_is_measured(self):
        assert summarise(self._obs("CALL", 15))["prompt_tokens_median"] == 500
