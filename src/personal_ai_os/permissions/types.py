"""The permission vocabulary.

The system must be able to tell these two things apart:

    a model *wants* to perform an action
    the user has *authorized* the action

``ToolCall`` is the first. ``PermissionDecision`` is the second. Nothing in
the codebase turns one into the other except the broker.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class PermissionLevel(str, Enum):
    """What class of consequence an action carries.

    String-valued so manifests and config read naturally; ordered via
    :attr:`severity` so policy can reason about "at least as dangerous as".
    """

    READ = "read"
    WRITE = "write"
    EXTERNAL_ACTION = "external_action"
    SEND_MESSAGE = "send_message"
    SPEND_MONEY = "spend_money"
    DELETE = "delete"
    DESTRUCTIVE = "destructive"

    @property
    def severity(self) -> int:
        return _SEVERITY[self]

    def at_least(self, other: PermissionLevel) -> bool:
        return self.severity >= other.severity


_SEVERITY: dict[PermissionLevel, int] = {
    PermissionLevel.READ: 10,
    PermissionLevel.WRITE: 20,
    PermissionLevel.EXTERNAL_ACTION: 30,
    PermissionLevel.SEND_MESSAGE: 40,
    PermissionLevel.SPEND_MONEY: 50,
    PermissionLevel.DELETE: 60,
    PermissionLevel.DESTRUCTIVE: 70,
}


class PermissionRequest(BaseModel):
    """A request to perform one consequential action, awaiting a decision."""

    level: PermissionLevel
    action: str
    resource: str = ""
    agent: str = ""
    run_id: str = ""
    detail: str = ""
    #: Set by tools that always need a human regardless of level policy.
    requires_human_approval: bool = False

    def summary(self) -> str:
        target = f" on {self.resource}" if self.resource else ""
        return f"{self.action}{target} [{self.level.value}]"


DecisionSource = Literal["policy", "user", "spec", "non_interactive"]


class PermissionDecision(BaseModel):
    """The authoritative answer. Only a broker may construct a granted one."""

    granted: bool
    reason: str = ""
    source: DecisionSource = "policy"
    request: PermissionRequest | None = Field(default=None, repr=False)

    @property
    def denied(self) -> bool:
        return not self.granted
