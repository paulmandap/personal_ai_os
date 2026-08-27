"""Configuration loading.

Three layers, lowest precedence first::

    config/default.yaml     committed, the shared baseline
    config/local.yaml       gitignored, this machine's overrides
    PAIOS_* env vars        per-invocation overrides

The merged result is validated into :class:`Settings`, so an unknown key is a
startup error rather than a silently ignored line in a YAML file.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from personal_ai_os.config.schema import Settings
from personal_ai_os.core.errors import ConfigError

ENV_PREFIX = "PAIOS_"
#: Separates nested keys in env vars: PAIOS_MODELS__OLLAMA__BASE_URL
ENV_NESTING = "__"

DEFAULT_CONFIG = Path("config/default.yaml")
LOCAL_CONFIG = Path("config/local.yaml")


def find_workspace_root(start: Path | None = None) -> Path:
    """Walk upward looking for the repository marker.

    Lets ``paios`` be run from any subdirectory and still find its config,
    which matters because agent runs are launched from wherever you happen
    to be standing.
    """
    env_root = os.environ.get(f"{ENV_PREFIX}WORKSPACE")
    if env_root:
        return Path(env_root).resolve()

    current = (start or Path.cwd()).resolve()
    for candidate in [current, *current.parents]:
        if (candidate / DEFAULT_CONFIG).is_file():
            return candidate
    raise ConfigError(
        f"could not locate {DEFAULT_CONFIG} in {current} or any parent directory; "
        f"set {ENV_PREFIX}WORKSPACE to point at the repository root"
    )


def deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``over`` onto ``base``, returning a new dict.

    Mappings merge key-by-key; every other type (including lists) is replaced
    wholesale. Replacing lists is the safer default -- a partially merged
    ``allowed_roots`` would silently widen the filesystem jail.
    """
    result = dict(base)
    for key, value in over.items():
        existing = result.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            result[key] = deep_merge(existing, value)
        else:
            result[key] = value
    return result


def _coerce_env_value(raw: str) -> Any:
    """Parse an env string with YAML rules so ints/bools/null arrive typed."""
    try:
        return yaml.safe_load(raw)
    except yaml.YAMLError:
        return raw


def env_overrides(env: dict[str, str] | None = None) -> dict[str, Any]:
    """Build a nested override dict from ``PAIOS_*`` environment variables."""
    env = os.environ if env is None else env
    overrides: dict[str, Any] = {}

    for key, raw in env.items():
        if not key.startswith(ENV_PREFIX):
            continue
        path = key[len(ENV_PREFIX) :].lower()
        if path == "workspace":  # handled by find_workspace_root
            continue
        parts = [p for p in path.split(ENV_NESTING.lower()) if p]
        if not parts:
            continue

        cursor = overrides
        for part in parts[:-1]:
            nxt = cursor.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                cursor[part] = nxt
            cursor = nxt
        cursor[parts[-1]] = _coerce_env_value(raw)

    return overrides


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a YAML mapping at the top level")
    return data


def load_settings(
    workspace_root: Path | None = None,
    *,
    overrides: dict[str, Any] | None = None,
    env: dict[str, str] | None = None,
    use_env: bool = True,
) -> Settings:
    """Load, merge and validate configuration.

    Args:
        workspace_root: Repository root. Discovered by walking up if omitted.
        overrides: Highest-precedence overrides, applied last (used by tests
            and by CLI flags).
        env: Environment mapping to read; defaults to ``os.environ``.
        use_env: Set false to ignore the environment entirely.
    """
    root = (workspace_root or find_workspace_root()).resolve()

    merged = _read_yaml(root / DEFAULT_CONFIG)
    merged = deep_merge(merged, _read_yaml(root / LOCAL_CONFIG))
    if use_env:
        merged = deep_merge(merged, env_overrides(env))
    if overrides:
        merged = deep_merge(merged, overrides)

    # The workspace root is derived, never configured: it is wherever the
    # config file was actually found.
    merged["workspace_root"] = str(root)

    try:
        return Settings.model_validate(merged)
    except ValidationError as exc:
        raise ConfigError(f"invalid configuration:\n{exc}") from exc
