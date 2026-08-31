"""Agent manifests: validation, discovery and entrypoint resolution.

The load-time checks here are the reason the registry is a safety boundary
rather than a lookup table.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from personal_ai_os.agents.base import BaseAgent
from personal_ai_os.agents.builtin.ping import PingAgent
from personal_ai_os.agents.registry import AgentRegistry
from personal_ai_os.agents.spec import AgentSpec
from personal_ai_os.core.errors import AgentNotFoundError, AgentSpecError
from personal_ai_os.permissions.types import PermissionLevel
from personal_ai_os.tools.registry import ToolRegistry, default_registry

VALID_MANIFEST = {
    "name": "reader",
    "description": "Reads files.",
    "tools": ["read_file"],
    "permissions": ["read"],
}


def write_manifest(agents_dir: Path, data: dict, filename: str = "a.yaml") -> Path:
    agents_dir.mkdir(parents=True, exist_ok=True)
    path = agents_dir / filename
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


@pytest.fixture
def tool_registry() -> ToolRegistry:
    return default_registry()


class TestSpecValidation:
    def test_entrypoint_must_be_module_colon_class(self):
        with pytest.raises(ValueError, match="module.path:ClassName"):
            AgentSpec(name="a", description="d", entrypoint="not_valid")

    def test_name_must_be_identifier_like(self):
        with pytest.raises(ValueError, match="alphanumeric"):
            AgentSpec(name="bad name!", description="d")

    def test_duplicate_tools_are_rejected(self):
        with pytest.raises(ValueError, match="duplicate"):
            AgentSpec(name="a", description="d", tools=["read_file", "read_file"])

    def test_unknown_manifest_key_is_rejected(self):
        with pytest.raises(ValueError):
            AgentSpec.model_validate(
                {"name": "a", "description": "d", "tolls": ["read_file"]}
            )

    def test_entrypoint_defaults_to_the_generic_loop(self):
        """An agent should be able to be pure configuration."""
        spec = AgentSpec(name="a", description="d")
        assert spec.entrypoint.endswith(":BaseAgent")


class TestRegistryValidation:
    def test_unknown_tool_is_refused_at_load_time(self, tool_registry):
        spec = AgentSpec(
            name="a", description="d", tools=["send_nukes"], permissions=["read"]
        )
        with pytest.raises(AgentSpecError, match="unregistered tool"):
            AgentRegistry([spec], tools=tool_registry)

    def test_undeclared_permission_is_refused_at_load_time(self, tool_registry):
        """The manifest must state its own blast radius, and be held to it."""
        spec = AgentSpec(name="a", description="d", tools=["read_file"], permissions=[])
        with pytest.raises(AgentSpecError, match="does not declare"):
            AgentRegistry([spec], tools=tool_registry)

    def test_declaring_the_permission_makes_it_load(self, tool_registry):
        spec = AgentSpec(
            name="a",
            description="d",
            tools=["read_file"],
            permissions=[PermissionLevel.READ],
        )
        assert len(AgentRegistry([spec], tools=tool_registry)) == 1

    def test_duplicate_agent_names_are_refused(self, tool_registry):
        spec = AgentSpec(name="a", description="d")
        with pytest.raises(AgentSpecError, match="more than once"):
            AgentRegistry([spec, spec.model_copy()], tools=tool_registry)


class TestLoadingFromDisk:
    def test_loads_every_manifest_in_the_directory(self, tmp_path, tool_registry):
        write_manifest(tmp_path / "agents", VALID_MANIFEST, "one.yaml")
        write_manifest(
            tmp_path / "agents", {**VALID_MANIFEST, "name": "second"}, "two.yaml"
        )
        registry = AgentRegistry.from_dir(tmp_path / "agents", tools=tool_registry)
        assert registry.names() == ["reader", "second"]

    def test_missing_directory_is_a_warning_not_a_crash(self, tmp_path, tool_registry):
        registry = AgentRegistry.from_dir(tmp_path / "nope", tools=tool_registry)
        assert len(registry) == 0

    def test_malformed_yaml_names_the_file(self, tmp_path, tool_registry):
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "bad.yaml").write_text("name: [unclosed\n", encoding="utf-8")
        with pytest.raises(AgentSpecError, match="bad.yaml"):
            AgentRegistry.from_dir(agents_dir, tools=tool_registry)

    def test_invalid_manifest_names_the_file(self, tmp_path, tool_registry):
        write_manifest(tmp_path / "agents", {"description": "no name"}, "x.yaml")
        with pytest.raises(AgentSpecError, match="x.yaml"):
            AgentRegistry.from_dir(tmp_path / "agents", tools=tool_registry)


class TestLookup:
    def test_unknown_agent_lists_what_exists(self, tool_registry):
        registry = AgentRegistry(
            [AgentSpec(name="known", description="d")], tools=tool_registry
        )
        with pytest.raises(AgentNotFoundError, match="known"):
            registry.get("missing")


class TestEntrypointResolution:
    def test_resolves_a_real_agent_class(self, tool_registry):
        spec = AgentSpec(
            name="ping",
            description="d",
            entrypoint="personal_ai_os.agents.builtin.ping:PingAgent",
        )
        registry = AgentRegistry([spec], tools=tool_registry)
        assert registry.resolve_class(spec) is PingAgent

    def test_default_entrypoint_resolves_to_base_agent(self, tool_registry):
        spec = AgentSpec(name="plain", description="d")
        registry = AgentRegistry([spec], tools=tool_registry)
        assert registry.resolve_class(spec) is BaseAgent

    def test_missing_module_is_reported_with_the_agent_name(self, tool_registry):
        spec = AgentSpec(name="a", description="d", entrypoint="no.such.module:Thing")
        registry = AgentRegistry([spec], tools=tool_registry)
        with pytest.raises(AgentSpecError, match="cannot import"):
            registry.resolve_class(spec)

    def test_missing_class_is_reported(self, tool_registry):
        spec = AgentSpec(
            name="a", description="d", entrypoint="personal_ai_os.agents.base:Nope"
        )
        registry = AgentRegistry([spec], tools=tool_registry)
        with pytest.raises(AgentSpecError, match="no attribute"):
            registry.resolve_class(spec)

    def test_non_agent_class_is_refused(self, tool_registry):
        spec = AgentSpec(
            name="a", description="d", entrypoint="personal_ai_os.agents.spec:AgentSpec"
        )
        registry = AgentRegistry([spec], tools=tool_registry)
        with pytest.raises(AgentSpecError, match="not a BaseAgent subclass"):
            registry.resolve_class(spec)


class TestSystemPrompt:
    def test_inline_prompt_is_used(self, tool_registry):
        spec = AgentSpec(name="a", description="d", system_prompt="inline")
        registry = AgentRegistry([spec], tools=tool_registry)
        assert registry.system_prompt_for(spec) == "inline"

    def test_prompt_file_is_read_relative_to_the_agents_dir(
        self, tmp_path, tool_registry
    ):
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "p.md").write_text("from file", encoding="utf-8")
        spec = AgentSpec(name="a", description="d", system_prompt_file="p.md")
        registry = AgentRegistry([spec], tools=tool_registry, agents_dir=agents_dir)
        assert registry.system_prompt_for(spec) == "from file"

    def test_missing_prompt_file_is_reported(self, tmp_path, tool_registry):
        spec = AgentSpec(name="a", description="d", system_prompt_file="gone.md")
        registry = AgentRegistry([spec], tools=tool_registry, agents_dir=tmp_path)
        with pytest.raises(AgentSpecError, match="system_prompt_file"):
            registry.system_prompt_for(spec)


class TestShippedManifests:
    """Guards the manifests that ship with the repo.

    These also record the answer to Phase 2's first open question: two new,
    independently-motivated agents were added without a single change to
    `AgentRegistry`. The abstraction held.
    """

    @pytest.fixture
    def shipped(self, tool_registry) -> AgentRegistry:
        repo_agents = Path(__file__).resolve().parents[2] / "agents"
        return AgentRegistry.from_dir(repo_agents, tools=tool_registry)

    def test_every_shipped_manifest_loads(self, shipped):
        assert shipped.names() == [
            "finance", "master", "ping", "research", "task_agent"
        ]

    def test_research_is_the_only_external_action_agent(self, shipped):
        """The first agent to reach outside the machine, and the reason it was
        gated on the whole safety programme. Pinned so the second one is a
        deliberate act rather than a diff nobody read."""
        external = [
            name for name in shipped.names()
            if PermissionLevel.EXTERNAL_ACTION in shipped.get(name).permissions
        ]
        assert external == ["research"]

    def test_research_holds_no_write_capability(self, shipped):
        """A capability boundary, and NOT evidence that it resists
        write-inducing injection -- there is no write to induce. The two are
        different properties, and state integrity only becomes measurable when
        a research path can write. When that arrives, its attack cases go in
        BEFORE the capability."""
        spec = shipped.get("research")
        assert spec.tools == ["fetch_page"]
        assert PermissionLevel.WRITE not in spec.permissions

    def test_finance_declares_write_but_not_spend_money(self, shipped):
        """These tools record facts about money; none of them move any."""
        spec = shipped.get("finance")
        assert PermissionLevel.WRITE in spec.permissions
        assert PermissionLevel.SPEND_MONEY not in spec.permissions
        assert "affordability_check" in spec.tools

    def test_ping_resolves_to_its_class(self, shipped):
        spec = shipped.get("ping")
        assert PermissionLevel.READ in spec.permissions
        assert shipped.resolve_class(spec) is PingAgent

    def test_task_agent_declares_write(self, shipped):
        """It mutates stored data, so the manifest must say so."""
        spec = shipped.get("task_agent")
        assert PermissionLevel.WRITE in spec.permissions
        assert set(spec.tools) == {
            "add_task",
            "list_tasks",
            "update_task",
            "complete_task",
        }

    def test_master_can_only_delegate(self, shipped):
        """Delegation discipline is structural: it has no other tools."""
        spec = shipped.get("master")
        assert spec.tools == ["delegate"]
        assert PermissionLevel.WRITE not in spec.permissions
