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


class TestShippedManifest:
    def test_the_real_ping_manifest_is_valid(self, tool_registry):
        """Guards the manifest that ships with the repo."""
        repo_agents = Path(__file__).resolve().parents[2] / "agents"
        registry = AgentRegistry.from_dir(repo_agents, tools=tool_registry)
        assert "ping" in registry.names()
        spec = registry.get("ping")
        assert PermissionLevel.READ in spec.permissions
        assert registry.resolve_class(spec) is PingAgent
