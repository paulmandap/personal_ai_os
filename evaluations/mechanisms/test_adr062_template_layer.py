"""Controls for the ADR-062 instrument.

Everything here is pure — `classify_raw` and the gate predicates take no network,
so this runs with Ollama stopped and sockets blocked. The HTTP lives in
`call_chat` / `call_raw`, which have nothing to decide.

The distinction these exist to protect: **a malformed or truncated tool call is
not the same observation as a clean one, and neither is the same as silence.**
ADR-059's whole finding was that a discarded detail is a lost mechanism, so the
classifier keeps the detail even though the primary endpoint does not use it.
"""

from __future__ import annotations

from adr062_template_layer import (
    SUPPRESSED_MAX,
    classify_raw,
    gate1_verdict,
    gate2_verdict,
    sha256,
    strip_end_tokens,
)

CLEAN = '<tool_call>\n{"name": "delegate", "arguments": {"agent": "task_agent", "objective": "Add a task"}}\n</tool_call>'


class TestPrimaryClass:
    def test_a_tool_call_block_is_TOOL_CALL_LIKE(self):
        assert classify_raw(CLEAN)["primary"] == "TOOL_CALL_LIKE"

    def test_bare_json_naming_a_function_is_TOOL_CALL_LIKE(self):
        """Some templates emit the object without the XML tags."""
        text = '{"name": "delegate", "arguments": {"agent": "task_agent", "objective": "x"}}'
        assert classify_raw(text)["primary"] == "TOOL_CALL_LIKE"

    def test_ordinary_text_is_PROSE(self):
        assert classify_raw("I will delegate this to the task agent.")["primary"] == "PROSE"

    def test_empty_is_EMPTY(self):
        assert classify_raw("")["primary"] == "EMPTY"

    def test_whitespace_only_is_EMPTY(self):
        assert classify_raw("   \n\t ")["primary"] == "EMPTY"

    def test_end_token_only_is_EMPTY(self):
        """The failure under investigation may look exactly like this."""
        assert classify_raw("<|im_end|>")["primary"] == "EMPTY"
        assert classify_raw("\n<|im_end|>\n")["primary"] == "EMPTY"
        assert classify_raw("<|endoftext|>")["primary"] == "EMPTY"

    def test_prose_that_merely_mentions_the_tool_is_PROSE(self):
        """Naming a tool in prose is not emitting a call — the ADR-060 lesson."""
        out = classify_raw('I would call delegate with agent "task_agent".')
        assert out["primary"] == "PROSE"
        assert out["valid_delegate_call"] is False


class TestDiagnosticFlags:
    def test_a_clean_call_sets_every_positive_flag(self):
        out = classify_raw(CLEAN)
        assert out["has_tool_call_tag"] is True
        assert out["closed_tool_call"] is True
        assert out["malformed_or_truncated"] is False
        assert out["valid_json_call"] is True
        assert out["tool_name"] == "delegate"
        assert out["wrong_tool_name"] is False
        assert out["valid_delegate_call"] is True

    def test_an_unclosed_tag_is_truncated_but_still_TOOL_CALL_LIKE(self):
        """A cut-off call is evidence of generation, not of silence."""
        text = '<tool_call>\n{"name": "delegate", "arguments": {"agent": "task'
        out = classify_raw(text)
        assert out["primary"] == "TOOL_CALL_LIKE"
        assert out["malformed_or_truncated"] is True
        assert out["valid_json_call"] is False
        assert out["valid_delegate_call"] is False

    def test_closed_tag_with_broken_json_is_malformed(self):
        out = classify_raw("<tool_call>\n{not json\n</tool_call>")
        assert out["primary"] == "TOOL_CALL_LIKE"
        assert out["closed_tool_call"] is True
        assert out["malformed_or_truncated"] is True
        assert out["valid_json_call"] is False

    def test_a_wrong_tool_name_is_flagged_and_not_a_valid_delegation(self):
        text = '<tool_call>\n{"name": "add_task", "arguments": {"title": "x"}}\n</tool_call>'
        out = classify_raw(text)
        assert out["tool_name"] == "add_task"
        assert out["wrong_tool_name"] is True
        assert out["valid_delegate_call"] is False

    def test_an_invented_agent_is_a_call_but_not_a_valid_delegation(self):
        """ADR-060 saw the 3B invent `passportRenewalAgent`."""
        text = (
            '<tool_call>\n{"name": "delegate", "arguments": '
            '{"agent": "passportRenewalAgent", "objective": "x"}}\n</tool_call>'
        )
        out = classify_raw(text)
        assert out["primary"] == "TOOL_CALL_LIKE"
        assert out["valid_json_call"] is True
        assert out["wrong_tool_name"] is False
        assert out["valid_delegate_call"] is False

    def test_an_empty_objective_is_not_a_valid_delegation(self):
        text = (
            '<tool_call>\n{"name": "delegate", "arguments": '
            '{"agent": "task_agent", "objective": "  "}}\n</tool_call>'
        )
        assert classify_raw(text)["valid_delegate_call"] is False

    def test_a_trailing_end_token_does_not_break_parsing(self):
        assert classify_raw(CLEAN + "<|im_end|>")["valid_delegate_call"] is True

    def test_agents_none_accepts_any_agent_name(self):
        text = (
            '<tool_call>\n{"name": "delegate", "arguments": '
            '{"agent": "anything", "objective": "x"}}\n</tool_call>'
        )
        assert classify_raw(text, agents=None)["valid_delegate_call"] is True


class TestGates:
    def test_gate1_passes_only_in_the_failing_regime(self):
        assert gate1_verdict(0) is True
        assert gate1_verdict(SUPPRESSED_MAX) is True

    def test_gate1_fails_when_the_model_is_calling(self):
        """A calling regime means there are no silent turns to inspect."""
        assert gate1_verdict(SUPPRESSED_MAX + 1) is False
        assert gate1_verdict(15) is False

    def test_gate2_requires_equal_prompt_token_counts(self):
        assert gate2_verdict(550, 550) is True
        assert gate2_verdict(550, 549) is False

    def test_gate2_fails_on_missing_counts_rather_than_passing_quietly(self):
        assert gate2_verdict(None, 550) is False
        assert gate2_verdict(550, None) is False
        assert gate2_verdict(None, None) is False


class TestHelpers:
    def test_strip_end_tokens_removes_both_forms(self):
        assert strip_end_tokens("a<|im_end|>b<|endoftext|>") == "ab"

    def test_sha256_is_stable_and_sensitive(self):
        assert sha256("abc") == sha256("abc")
        assert sha256("abc") != sha256("abd")
        assert len(sha256("abc")) == 64
