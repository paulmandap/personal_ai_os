"""The permission gate.

The property under test throughout: nothing is authorised except by a broker,
and a refusal is always a *result*, never an exception.
"""

from __future__ import annotations

import pytest

from personal_ai_os.permissions.broker import (
    AllowAllBroker,
    DenyAllBroker,
    PolicyBroker,
    RecordingBroker,
)
from personal_ai_os.permissions.types import (
    PermissionLevel,
    PermissionRequest,
)

FULL_POLICY = {
    PermissionLevel.READ: "auto",
    PermissionLevel.WRITE: "ask",
    PermissionLevel.EXTERNAL_ACTION: "ask",
    PermissionLevel.SEND_MESSAGE: "ask",
    PermissionLevel.SPEND_MONEY: "ask",
    PermissionLevel.DELETE: "ask",
    PermissionLevel.DESTRUCTIVE: "deny",
}


def req(level: PermissionLevel, **kwargs) -> PermissionRequest:
    return PermissionRequest(level=level, action="do_thing", **kwargs)


class TestSeverity:
    def test_levels_are_ordered_by_consequence(self):
        assert PermissionLevel.READ.severity < PermissionLevel.WRITE.severity
        assert PermissionLevel.DESTRUCTIVE.severity > PermissionLevel.DELETE.severity

    def test_at_least_compares_by_severity(self):
        assert PermissionLevel.DELETE.at_least(PermissionLevel.READ)
        assert not PermissionLevel.READ.at_least(PermissionLevel.DELETE)


class TestPolicyMatrix:
    @pytest.mark.parametrize(
        ("level", "expected"),
        [
            (PermissionLevel.READ, True),
            (PermissionLevel.WRITE, False),
            (PermissionLevel.SEND_MESSAGE, False),
            (PermissionLevel.SPEND_MONEY, False),
            (PermissionLevel.DELETE, False),
            (PermissionLevel.DESTRUCTIVE, False),
        ],
    )
    def test_non_interactive_grants_only_auto_levels(self, level, expected):
        broker = PolicyBroker(FULL_POLICY, interactive=False)
        assert broker.request(req(level)).granted is expected

    def test_ask_prompts_and_honours_a_yes(self):
        broker = PolicyBroker(FULL_POLICY, interactive=True, prompt=lambda _r: True)
        decision = broker.request(req(PermissionLevel.WRITE))
        assert decision.granted
        assert decision.source == "user"

    def test_ask_prompts_and_honours_a_no(self):
        broker = PolicyBroker(FULL_POLICY, interactive=True, prompt=lambda _r: False)
        decision = broker.request(req(PermissionLevel.WRITE))
        assert decision.denied
        assert decision.source == "user"

    def test_deny_never_prompts(self):
        prompted = []
        broker = PolicyBroker(
            FULL_POLICY,
            interactive=True,
            prompt=lambda r: prompted.append(r) or True,
        )
        assert broker.request(req(PermissionLevel.DESTRUCTIVE)).denied
        assert prompted == []

    def test_unknown_level_defaults_to_deny(self):
        """A level nobody configured is not one to grant by default."""
        broker = PolicyBroker({}, interactive=False)
        assert broker.request(req(PermissionLevel.READ)).denied


class TestHumanApprovalOverride:
    def test_requires_human_approval_beats_an_auto_policy(self):
        prompted: list[PermissionRequest] = []
        broker = PolicyBroker(
            FULL_POLICY,
            interactive=True,
            prompt=lambda r: bool(prompted.append(r)) or True,
        )
        decision = broker.request(
            req(PermissionLevel.READ, requires_human_approval=True)
        )
        assert len(prompted) == 1
        assert decision.source == "user"

    def test_non_interactive_refuses_rather_than_self_approving(self):
        """An unattended run must never grant itself what needs a human."""
        broker = PolicyBroker(FULL_POLICY, interactive=False)
        decision = broker.request(
            req(PermissionLevel.READ, requires_human_approval=True)
        )
        assert decision.denied
        assert decision.source == "non_interactive"

    def test_interactive_without_a_prompter_refuses(self):
        broker = PolicyBroker(FULL_POLICY, interactive=True, prompt=None)
        assert broker.request(req(PermissionLevel.WRITE)).denied


class TestTestDoubles:
    def test_allow_all_and_deny_all(self):
        assert AllowAllBroker().request(req(PermissionLevel.DESTRUCTIVE)).granted
        assert DenyAllBroker().request(req(PermissionLevel.READ)).denied

    def test_recording_broker_captures_both_sides(self):
        broker = RecordingBroker(AllowAllBroker())
        broker.request(req(PermissionLevel.READ, resource="/tmp/x"))
        assert len(broker.requests) == 1
        assert broker.requests[0].resource == "/tmp/x"
        assert broker.decisions[0].granted


class TestRequestSummary:
    def test_summary_names_action_resource_and_level(self):
        text = req(PermissionLevel.DELETE, resource="/tmp/x").summary()
        assert "do_thing" in text and "/tmp/x" in text and "delete" in text
