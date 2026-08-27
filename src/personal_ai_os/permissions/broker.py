"""Permission brokers -- the only place an action becomes authorised.

Nothing else in the codebase may construct a granted :class:`PermissionDecision`.
That is the whole mechanism: a single choke point means "did the user allow
this?" has exactly one answer, and it is auditable.
"""

from __future__ import annotations

import sys
from abc import ABC, abstractmethod
from collections.abc import Callable

from personal_ai_os.config.schema import PolicyAction
from personal_ai_os.observability.logging import get_logger
from personal_ai_os.permissions.types import (
    PermissionDecision,
    PermissionLevel,
    PermissionRequest,
)

log = get_logger("permissions")

Prompter = Callable[[PermissionRequest], bool]

#: Applied when a level is absent from configuration. Deny, not allow: a
#: permission level nobody thought about is not one to grant by default.
FALLBACK_ACTION: PolicyAction = "deny"


class PermissionBroker(ABC):
    """Decides whether one requested action may proceed."""

    @abstractmethod
    def request(self, req: PermissionRequest) -> PermissionDecision:
        """Return a decision. Must never raise -- a refusal is a result."""


class PolicyBroker(PermissionBroker):
    """Applies the configured per-level policy.

    ``auto`` proceeds, ``deny`` refuses, and ``ask`` defers to a prompter. Two
    things override a permissive policy:

    * ``requires_human_approval`` on the request forces a prompt even where
      policy says ``auto`` -- some tools are dangerous regardless of level.
    * a non-interactive session downgrades every ``ask`` to a refusal, so an
      unattended run can never block on stdin nor silently self-approve.
    """

    def __init__(
        self,
        policy: dict[PermissionLevel, PolicyAction] | None = None,
        *,
        interactive: bool = True,
        prompt: Prompter | None = None,
    ) -> None:
        self._policy = dict(policy or {})
        self._interactive = interactive
        self._prompt = prompt

    def action_for(self, level: PermissionLevel) -> PolicyAction:
        return self._policy.get(level, FALLBACK_ACTION)

    def request(self, req: PermissionRequest) -> PermissionDecision:
        action = self.action_for(req.level)

        if action == "deny":
            return self._decide(
                req, False, "policy", f"policy denies {req.level.value} actions"
            )

        if action == "auto" and not req.requires_human_approval:
            return self._decide(
                req, True, "policy", f"policy auto-approves {req.level.value} actions"
            )

        # Either policy says 'ask', or the tool insists on a human.
        why = (
            "tool requires human approval"
            if req.requires_human_approval and action == "auto"
            else f"policy requires approval for {req.level.value} actions"
        )

        if not self._interactive or self._prompt is None:
            return self._decide(
                req,
                False,
                "non_interactive",
                f"{why}, but this session is non-interactive so it was refused",
            )

        approved = self._prompt(req)
        return self._decide(
            req,
            approved,
            "user",
            "approved by user" if approved else "declined by user",
        )

    @staticmethod
    def _decide(
        req: PermissionRequest, granted: bool, source: str, reason: str
    ) -> PermissionDecision:
        log.debug(
            "%s %s -- %s", "GRANT" if granted else "DENY ", req.summary(), reason
        )
        return PermissionDecision(
            granted=granted, reason=reason, source=source, request=req
        )


def cli_prompt(req: PermissionRequest) -> bool:
    """Ask on the terminal. Anything that is not an explicit yes is a no."""
    lines = [
        "",
        "  ┌─ approval required ─────────────────────────────────────────",
        f"  │ action   : {req.action}",
        f"  │ level    : {req.level.value}",
    ]
    if req.resource:
        lines.append(f"  │ resource : {req.resource}")
    if req.agent:
        lines.append(f"  │ agent    : {req.agent}")
    if req.detail:
        lines.append(f"  │ detail   : {req.detail}")
    lines.append("  └─────────────────────────────────────────────────────────────")
    print("\n".join(lines), file=sys.stderr)

    try:
        answer = input("  allow? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("", file=sys.stderr)
        return False
    return answer in {"y", "yes"}


class CLIPermissionBroker(PolicyBroker):
    """The interactive default: policy first, terminal prompt for anything left."""

    def __init__(
        self,
        policy: dict[PermissionLevel, PolicyAction] | None = None,
        *,
        interactive: bool = True,
    ) -> None:
        super().__init__(policy, interactive=interactive, prompt=cli_prompt)


# --- Test doubles ----------------------------------------------------------


class AllowAllBroker(PermissionBroker):
    """Grants everything. Tests only -- never wire this into a real run."""

    def request(self, req: PermissionRequest) -> PermissionDecision:
        return PermissionDecision(
            granted=True, reason="allow-all broker", source="policy", request=req
        )


class DenyAllBroker(PermissionBroker):
    """Refuses everything, for asserting that refusals are handled gracefully."""

    def __init__(self, reason: str = "deny-all broker") -> None:
        self._reason = reason

    def request(self, req: PermissionRequest) -> PermissionDecision:
        return PermissionDecision(
            granted=False, reason=self._reason, source="policy", request=req
        )


class RecordingBroker(PermissionBroker):
    """Wraps another broker and remembers every request and decision."""

    def __init__(self, inner: PermissionBroker) -> None:
        self._inner = inner
        self.requests: list[PermissionRequest] = []
        self.decisions: list[PermissionDecision] = []

    def request(self, req: PermissionRequest) -> PermissionDecision:
        self.requests.append(req)
        decision = self._inner.request(req)
        self.decisions.append(decision)
        return decision
