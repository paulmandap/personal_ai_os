"""Model routing.

Deliberately deterministic. A model that picks the model is a second thing to
debug when the first thing misbehaves, and until there is evaluation data
saying otherwise there is no evidence a learned router would do better. This
one is a lookup table with a documented precedence order -- easy to reason
about, trivial to test, and honest about what it does not know.

Resolution precedence, highest first::

    1. an explicit model name on the agent spec   ("use exactly this")
    2. a named role on the agent spec             ("this is planning work")
    3. a task complexity assessment               ("this looks hard")
    4. the configured default tier

Degradation only ever moves *down* the local ladder. There is no cloud rung to
fall off onto, by design (docs/decisions.md, ADR-001).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from personal_ai_os.config.schema import Tier
from personal_ai_os.core.errors import ModelNotConfiguredError
from personal_ai_os.core.model import AgentModel
from personal_ai_os.models.registry import TIER_ORDER, ModelRegistry
from personal_ai_os.observability.logging import get_logger

log = get_logger("router")


class TaskComplexity(str, Enum):
    """How much model a task is judged to need."""

    SIMPLE = "simple"
    NORMAL = "normal"
    COMPLEX = "complex"


COMPLEXITY_TIER: dict[TaskComplexity, Tier] = {
    TaskComplexity.SIMPLE: "small",
    TaskComplexity.NORMAL: "medium",
    TaskComplexity.COMPLEX: "large",
}


@dataclass(frozen=True)
class ModelSelection:
    """A routing decision, with its reasoning attached.

    The reasoning travels with the decision so it can be written to the trace.
    "Which model answered this, and why that one?" is the first question asked
    of any surprising agent output.
    """

    model: AgentModel
    tier: Tier | None
    requested_tier: Tier | None
    reason: str
    degraded: bool = False

    def as_trace(self) -> dict[str, object]:
        return {
            "model": self.model.name,
            "provider": self.model.provider,
            "tier": self.tier,
            "requested_tier": self.requested_tier,
            "reason": self.reason,
            "degraded": self.degraded,
        }


class ModelRouter:
    """Chooses which local model handles a given piece of work."""

    def __init__(self, registry: ModelRegistry) -> None:
        self._registry = registry

    def select(
        self,
        *,
        model_name: str | None = None,
        role: str | None = None,
        tier: Tier | None = None,
        complexity: TaskComplexity | None = None,
    ) -> ModelSelection:
        # 1. Explicit model wins outright.
        if model_name:
            return ModelSelection(
                model=self._registry.get_named(model_name, tier=tier),
                tier=tier,
                requested_tier=tier,
                reason=f"explicit model {model_name!r}",
            )

        # 2. Named role, 3. complexity, 4. configured default.
        if role:
            requested = self._registry.role_tier(role)
            if requested is None:
                raise ModelNotConfiguredError(
                    f"role {role!r} is not mapped to a tier; add it under models.roles"
                )
            reason = f"role {role!r} -> tier {requested!r}"
        elif tier:
            requested, reason = tier, f"explicit tier {tier!r}"
        elif complexity:
            requested = COMPLEXITY_TIER[complexity]
            reason = f"complexity {complexity.value!r} -> tier {requested!r}"
        else:
            requested = self._registry.settings.default_tier
            reason = f"default tier {requested!r}"

        resolved = self._degrade(requested)
        if resolved != requested:
            reason = (
                f"{reason}; tier {requested!r} is unmapped on this machine, "
                f"degraded to {resolved!r}"
            )
            log.warning(
                "tier %r has no model configured, degrading to %r", requested, resolved
            )

        return ModelSelection(
            model=self._registry.get_tier(resolved),
            tier=resolved,
            requested_tier=requested,
            reason=reason,
            degraded=resolved != requested,
        )

    def _degrade(self, requested: Tier) -> Tier:
        """Walk down the ladder to the first tier that has a model."""
        if self._registry.is_mapped(requested):
            return requested

        start = TIER_ORDER.index(requested)
        for candidate in TIER_ORDER[start + 1 :]:
            if self._registry.is_mapped(candidate):
                return candidate

        raise ModelNotConfiguredError(
            f"no model is mapped at tier {requested!r} or any tier below it. "
            f"Configure at least models.tiers.small.model."
        )
