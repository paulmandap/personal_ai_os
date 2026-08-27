"""Model registry and deterministic routing."""

from __future__ import annotations

import pytest

from personal_ai_os.config.schema import ModelSettings, TierSettings
from personal_ai_os.core.errors import ModelNotConfiguredError
from personal_ai_os.models.fake import ScriptedModel
from personal_ai_os.models.registry import ModelRegistry
from personal_ai_os.models.router import ModelRouter, TaskComplexity


def fake_factory(model_name, tier, settings):
    """Build a ScriptedModel so no HTTP client is ever constructed."""
    return ScriptedModel(name=model_name)


def make_registry(**overrides) -> ModelRegistry:
    settings = ModelSettings(
        tiers={
            "small": TierSettings(model="small-model"),
            "medium": TierSettings(model="medium-model"),
            "large": TierSettings(model=None),
        },
        roles={"classify": "small", "plan": "medium", "reason": "large"},
        default_tier="small",
        **overrides,
    )
    return ModelRegistry(settings, factory=fake_factory)


@pytest.fixture
def router() -> ModelRouter:
    return ModelRouter(make_registry())


class TestRegistry:
    def test_unmapped_tier_reports_as_unmapped(self):
        registry = make_registry()
        assert registry.is_mapped("medium") is True
        assert registry.is_mapped("large") is False

    def test_instances_are_cached(self):
        registry = make_registry()
        assert registry.get_tier("small") is registry.get_tier("small")

    def test_describe_lists_the_ladder_most_capable_first(self):
        tiers = [info.tier for info in make_registry().describe()]
        assert tiers == ["large", "medium", "small"]

    def test_requesting_an_unmapped_tier_directly_is_an_error(self):
        with pytest.raises(ModelNotConfiguredError, match="large"):
            make_registry().get_tier("large")

    def test_unknown_role_is_an_error(self):
        with pytest.raises(ModelNotConfiguredError, match="nope"):
            make_registry().get_role("nope")


class TestPrecedence:
    """Explicit model > role > tier > complexity > default."""

    def test_explicit_model_wins(self, router: ModelRouter):
        selection = router.select(model_name="pinned", role="plan", tier="small")
        assert selection.model.name == "pinned"
        assert "explicit model" in selection.reason

    def test_role_beats_complexity(self, router: ModelRouter):
        selection = router.select(role="classify", complexity=TaskComplexity.COMPLEX)
        assert selection.model.name == "small-model"
        assert selection.tier == "small"

    def test_complexity_maps_to_a_tier(self, router: ModelRouter):
        assert router.select(complexity=TaskComplexity.NORMAL).tier == "medium"
        assert router.select(complexity=TaskComplexity.SIMPLE).tier == "small"

    def test_falls_back_to_the_default_tier(self, router: ModelRouter):
        selection = router.select()
        assert selection.tier == "small"
        assert "default tier" in selection.reason


class TestDegradation:
    def test_unmapped_large_degrades_to_medium(self, router: ModelRouter):
        selection = router.select(complexity=TaskComplexity.COMPLEX)
        assert selection.requested_tier == "large"
        assert selection.tier == "medium"
        assert selection.degraded is True
        assert "degraded" in selection.reason

    def test_a_role_pointing_at_an_unmapped_tier_also_degrades(self, router):
        selection = router.select(role="reason")
        assert selection.tier == "medium"
        assert selection.degraded is True

    def test_degrades_past_several_unmapped_tiers(self):
        settings = ModelSettings(
            tiers={
                "small": TierSettings(model="only-model"),
                "medium": TierSettings(model=None),
                "large": TierSettings(model=None),
            },
            default_tier="small",
        )
        router = ModelRouter(ModelRegistry(settings, factory=fake_factory))
        assert router.select(complexity=TaskComplexity.COMPLEX).tier == "small"

    def test_never_degrades_upward(self, router: ModelRouter):
        """Degrading up would silently spend more compute than asked for."""
        selection = router.select(complexity=TaskComplexity.SIMPLE)
        assert selection.tier == "small"
        assert selection.degraded is False

    def test_nothing_mapped_at_all_is_an_error_not_a_silent_fallback(self):
        settings = ModelSettings(
            tiers={"small": TierSettings(model=None)}, default_tier="small"
        )
        router = ModelRouter(ModelRegistry(settings, factory=fake_factory))
        with pytest.raises(ModelNotConfiguredError, match="tiers.small.model|below it"):
            router.select()


class TestSelectionTrace:
    def test_selection_carries_its_reasoning(self, router: ModelRouter):
        """'Which model answered, and why that one?' must be answerable."""
        payload = router.select(role="plan").as_trace()
        assert payload["model"] == "medium-model"
        assert payload["tier"] == "medium"
        assert payload["reason"]
        assert payload["degraded"] is False
