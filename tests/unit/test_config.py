"""Configuration loading, merging and validation."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from personal_ai_os.config.loader import (
    deep_merge,
    env_overrides,
    find_workspace_root,
    load_settings,
)
from personal_ai_os.core.errors import ConfigError
from personal_ai_os.permissions.types import PermissionLevel
from tests.conftest import BASE_CONFIG, write_config


class TestDeepMerge:
    def test_merges_nested_mappings_key_by_key(self):
        base = {"a": {"x": 1, "y": 2}, "b": 3}
        assert deep_merge(base, {"a": {"y": 99}}) == {"a": {"x": 1, "y": 99}, "b": 3}

    def test_lists_are_replaced_not_concatenated(self):
        """A partially merged allowed_roots would silently widen the jail."""
        merged = deep_merge({"roots": ["a", "b"]}, {"roots": ["c"]})
        assert merged["roots"] == ["c"]

    def test_does_not_mutate_its_inputs(self):
        base = {"a": {"x": 1}}
        original = copy.deepcopy(base)
        deep_merge(base, {"a": {"x": 2}})
        assert base == original


class TestEnvOverrides:
    def test_double_underscore_nests(self):
        env = {"PAIOS_MODELS__OLLAMA__BASE_URL": "http://box:1234"}
        assert env_overrides(env) == {
            "models": {"ollama": {"base_url": "http://box:1234"}}
        }

    def test_values_are_parsed_not_left_as_strings(self):
        env = {
            "PAIOS_MODELS__TIERS__SMALL__NUM_CTX": "2048",
            "PAIOS_OBSERVABILITY__TRACE_ENABLED": "false",
            "PAIOS_MODELS__TIERS__LARGE__MODEL": "null",
        }
        result = env_overrides(env)
        assert result["models"]["tiers"]["small"]["num_ctx"] == 2048
        assert result["observability"]["trace_enabled"] is False
        assert result["models"]["tiers"]["large"]["model"] is None

    def test_ignores_unprefixed_and_workspace(self):
        env = {"PATH": "/usr/bin", "PAIOS_WORKSPACE": "/somewhere"}
        assert env_overrides(env) == {}


class TestLayering:
    def test_local_overrides_default(self, workspace: Path):
        (workspace / "config" / "local.yaml").write_text(
            yaml.safe_dump({"models": {"tiers": {"small": {"model": "local-choice"}}}}),
            encoding="utf-8",
        )
        settings = load_settings(workspace, use_env=False)
        assert settings.models.tiers["small"].model == "local-choice"
        # Untouched keys survive the merge.
        assert settings.models.tiers["small"].num_ctx == 4096

    def test_env_overrides_local(self, workspace: Path):
        (workspace / "config" / "local.yaml").write_text(
            yaml.safe_dump({"models": {"tiers": {"small": {"model": "from-local"}}}}),
            encoding="utf-8",
        )
        settings = load_settings(
            workspace,
            env={"PAIOS_MODELS__TIERS__SMALL__MODEL": "from-env"},
        )
        assert settings.models.tiers["small"].model == "from-env"

    def test_workspace_root_is_derived_not_configured(self, workspace: Path):
        settings = load_settings(workspace, use_env=False)
        assert settings.workspace_root == workspace.resolve()


class TestValidation:
    def test_unknown_key_is_a_startup_error(self, tmp_path: Path):
        config = copy.deepcopy(BASE_CONFIG)
        config["modles"] = {}  # typo
        write_config(tmp_path, config)
        with pytest.raises(ConfigError, match="modles"):
            load_settings(tmp_path, use_env=False)

    def test_missing_small_tier_is_rejected(self, tmp_path: Path):
        """small is the floor the router degrades to; without it nothing works."""
        config = copy.deepcopy(BASE_CONFIG)
        del config["models"]["tiers"]["small"]
        write_config(tmp_path, config)
        with pytest.raises(ConfigError, match="small"):
            load_settings(tmp_path, use_env=False)

    def test_malformed_yaml_names_the_file(self, tmp_path: Path):
        (tmp_path / "config").mkdir()
        (tmp_path / "config" / "default.yaml").write_text("a: [1, 2\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="not valid YAML"):
            load_settings(tmp_path, use_env=False)

    def test_permission_levels_parse_from_strings(self, settings):
        assert settings.permissions.policy[PermissionLevel.READ] == "auto"
        assert settings.permissions.policy[PermissionLevel.DESTRUCTIVE] == "deny"


class TestWorkspaceDiscovery:
    def test_walks_up_from_a_subdirectory(self, workspace: Path):
        nested = workspace / "src" / "deep" / "deeper"
        nested.mkdir(parents=True)
        assert find_workspace_root(nested) == workspace.resolve()

    def test_reports_clearly_when_not_found(self, tmp_path: Path):
        with pytest.raises(ConfigError, match="PAIOS_WORKSPACE"):
            find_workspace_root(tmp_path)


class TestResolvedPaths:
    def test_allowed_roots_become_absolute(self, settings):
        roots = settings.resolved_allowed_roots()
        assert all(r.is_absolute() for r in roots)
        assert roots == [settings.workspace_root.resolve()]
