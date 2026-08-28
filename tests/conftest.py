"""Shared fixtures.

Every fixture here builds a self-contained workspace in ``tmp_path``. No test
in ``tests/unit`` reads the real repository configuration, touches the real
``runs/`` directory, or needs Ollama running.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from personal_ai_os.agents.spec import AgentSpec, ModelPreference
from personal_ai_os.config.loader import load_settings
from personal_ai_os.config.schema import Settings
from personal_ai_os.memory.store import Store
from personal_ai_os.memory.tasks import TaskStore
from personal_ai_os.permissions.types import PermissionLevel
from personal_ai_os.tools.base import ToolContext
from personal_ai_os.tools.registry import ToolRegistry, default_registry

BASE_CONFIG: dict[str, Any] = {
    "models": {
        "provider": "ollama",
        "ollama": {"base_url": "http://localhost:11434", "request_timeout_s": 30},
        "tiers": {
            "small": {"model": "small-model", "temperature": 0.1, "num_ctx": 4096},
            "medium": {"model": "medium-model", "temperature": 0.3, "num_ctx": 8192},
            "large": {"model": None},
        },
        "roles": {"classify": "small", "plan": "medium", "reason": "large"},
        "default_tier": "small",
    },
    "permissions": {
        "interactive": False,
        "policy": {
            "read": "auto",
            "write": "ask",
            "external_action": "ask",
            "send_message": "ask",
            "spend_money": "ask",
            "delete": "ask",
            "destructive": "deny",
        },
    },
    "paths": {"allowed_roots": ["."], "agents_dir": "agents", "runs_dir": "runs"},
    "observability": {"log_level": "DEBUG", "trace_enabled": False},
    "agent_defaults": {"max_iterations": 4, "timeout_s": 60},
}


def write_config(root: Path, config: dict[str, Any] | None = None) -> Path:
    """Write a config/default.yaml into ``root``."""
    cfg_dir = root / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    path = cfg_dir / "default.yaml"
    path.write_text(
        yaml.safe_dump(config if config is not None else BASE_CONFIG), encoding="utf-8"
    )
    return path


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A minimal but complete workspace: config, agents dir, a file to read."""
    write_config(tmp_path)
    (tmp_path / "agents").mkdir(exist_ok=True)
    (tmp_path / "README.md").write_text(
        "# Test Workspace\n\nA fixture repository.\n", encoding="utf-8"
    )
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "src" / "app.py").write_text("VALUE = 42\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def settings(workspace: Path) -> Settings:
    # use_env=False so a stray PAIOS_* variable in the developer's shell can
    # never change what a test asserts.
    return load_settings(workspace, use_env=False)


@pytest.fixture
def tools() -> ToolRegistry:
    return default_registry()


@pytest.fixture
def store() -> Iterator[Store]:
    """An in-memory database. Never touches the filesystem."""
    db = Store.in_memory()
    yield db
    db.close()


@pytest.fixture
def tasks(store: Store) -> TaskStore:
    return TaskStore(store)


@pytest.fixture
def tool_context(settings: Settings, store: Store) -> ToolContext:
    return ToolContext(
        allowed_roots=settings.resolved_allowed_roots(),
        workspace_root=settings.workspace_root.resolve(),
        agent="test_agent",
        run_id="testrun",
        store=store,
        max_delegation_depth=2,
        call_stack=("test_agent",),
    )


@pytest.fixture
def read_spec() -> AgentSpec:
    """An agent that may read files -- the common case under test."""
    return AgentSpec(
        name="reader",
        description="Reads files in the workspace.",
        model=ModelPreference(tier="small"),
        tools=["read_file", "list_dir"],
        permissions=[PermissionLevel.READ],
        max_iterations=4,
    )
