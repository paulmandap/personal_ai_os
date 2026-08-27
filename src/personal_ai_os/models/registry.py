"""Model registry -- turns configuration into live :class:`AgentModel` objects.

Models are built lazily and cached, so a process that only ever uses the small
tier never constructs a client for the others, and repeated lookups return the
same warm instance.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from personal_ai_os.config.schema import ModelSettings, Tier, TierSettings
from personal_ai_os.core.errors import ModelNotConfiguredError
from personal_ai_os.core.model import AgentModel
from personal_ai_os.core.types import ModelHealth
from personal_ai_os.models.ollama import OllamaModel

#: Ladder order, most capable first. Routing degrades along this order.
TIER_ORDER: tuple[Tier, ...] = ("large", "medium", "small")

ModelFactory = Callable[[str, TierSettings, ModelSettings], AgentModel]


def default_factory(
    model_name: str, tier: TierSettings, settings: ModelSettings
) -> AgentModel:
    """Build the provider named in configuration.

    The only place in the codebase that names a concrete provider class. A new
    local backend is added here and nowhere else.
    """
    if settings.provider == "ollama":
        return OllamaModel(
            model_name,
            base_url=settings.ollama.base_url,
            temperature=tier.temperature,
            num_ctx=tier.num_ctx,
            request_timeout_s=settings.ollama.request_timeout_s,
            keep_alive=settings.ollama.keep_alive,
        )
    raise ModelNotConfiguredError(f"unknown provider: {settings.provider!r}")


@dataclass(frozen=True)
class TierInfo:
    """What ``paios models`` prints."""

    tier: Tier
    model: str | None
    temperature: float
    num_ctx: int

    @property
    def mapped(self) -> bool:
        return self.model is not None


class ModelRegistry:
    """Resolves tiers, roles and explicit model names to model instances."""

    def __init__(
        self, settings: ModelSettings, *, factory: ModelFactory = default_factory
    ) -> None:
        self._settings = settings
        self._factory = factory
        self._cache: dict[str, AgentModel] = {}

    # --- inspection --------------------------------------------------------

    @property
    def settings(self) -> ModelSettings:
        return self._settings

    def tier_settings(self, tier: Tier) -> TierSettings:
        cfg = self._settings.tiers.get(tier)
        if cfg is None:
            raise ModelNotConfiguredError(f"tier {tier!r} is not present in config")
        return cfg

    def is_mapped(self, tier: Tier) -> bool:
        """True when a tier has a model behind it.

        An unmapped tier is a legitimate configuration -- it says "this machine
        cannot serve that rung" -- and the router degrades rather than failing.
        """
        cfg = self._settings.tiers.get(tier)
        return cfg is not None and cfg.model is not None

    def describe(self) -> list[TierInfo]:
        return [
            TierInfo(
                tier=tier,
                model=cfg.model,
                temperature=cfg.temperature,
                num_ctx=cfg.num_ctx,
            )
            for tier in TIER_ORDER
            if (cfg := self._settings.tiers.get(tier)) is not None
        ]

    def health_report(self) -> list[tuple[Tier, ModelHealth | None]]:
        """Health per mapped tier. Unmapped tiers report ``None``."""
        report: list[tuple[Tier, ModelHealth | None]] = []
        for info in self.describe():
            if not info.mapped:
                report.append((info.tier, None))
            else:
                report.append((info.tier, self.get_tier(info.tier).health()))
        return report

    # --- resolution --------------------------------------------------------

    def get_tier(self, tier: Tier) -> AgentModel:
        cfg = self.tier_settings(tier)
        if cfg.model is None:
            raise ModelNotConfiguredError(
                f"tier {tier!r} has no model mapped; set models.tiers.{tier}.model "
                f"in config, or let the router degrade to a lower tier"
            )
        return self._build(cfg.model, cfg)

    def get_role(self, role: str) -> AgentModel:
        tier = self._settings.roles.get(role)
        if tier is None:
            raise ModelNotConfiguredError(
                f"role {role!r} is not mapped to a tier; add it under models.roles"
            )
        return self.get_tier(tier)

    def role_tier(self, role: str) -> Tier | None:
        return self._settings.roles.get(role)

    def get_named(self, model_name: str, *, tier: Tier | None = None) -> AgentModel:
        """Build an explicitly named model, borrowing a tier's sampling settings."""
        base = self._settings.tiers.get(tier or self._settings.default_tier)
        return self._build(model_name, base or TierSettings())

    def _build(self, model_name: str, cfg: TierSettings) -> AgentModel:
        key = f"{model_name}|{cfg.temperature}|{cfg.num_ctx}"
        if key not in self._cache:
            self._cache[key] = self._factory(model_name, cfg, self._settings)
        return self._cache[key]

    def close(self) -> None:
        for model in self._cache.values():
            closer = getattr(model, "close", None)
            if callable(closer):
                closer()
        self._cache.clear()
