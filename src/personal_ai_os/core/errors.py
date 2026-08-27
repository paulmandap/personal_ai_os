"""Typed exceptions.

Every failure the system can produce on purpose has a class here. Agents
distinguish *recoverable* failures (bad tool arguments, a denied permission --
these are fed back to the model so it can re-plan) from *fatal* ones (the
inference server is down), and that distinction is only legible if failures
are typed rather than stringly.
"""

from __future__ import annotations


class PersonalAIOSError(Exception):
    """Base class for every error this system raises deliberately."""


# --- Configuration ---------------------------------------------------------


class ConfigError(PersonalAIOSError):
    """Configuration is missing, malformed, or fails validation."""


# --- Models ----------------------------------------------------------------


class ModelError(PersonalAIOSError):
    """Base class for inference failures."""


class ModelUnavailableError(ModelError):
    """The inference server is unreachable, or the model is not installed."""


class ModelTimeoutError(ModelError):
    """The model did not respond within its timeout."""


class ModelProtocolError(ModelError):
    """The server replied with something we cannot parse into a ModelResponse."""


class ModelNotConfiguredError(ModelError):
    """A tier or role was requested that no local model is mapped to."""


# --- Tools -----------------------------------------------------------------


class ToolError(PersonalAIOSError):
    """Base class for tool failures."""


class ToolNotFoundError(ToolError):
    """The model asked for a tool that is not registered or not exposed."""


class ToolInputError(ToolError):
    """Tool arguments failed validation. Recoverable: reported back to the model."""


class ToolExecutionError(ToolError):
    """The tool ran and failed."""


class ToolTimeoutError(ToolError):
    """The tool exceeded its timeout."""


class PathNotAllowedError(ToolError):
    """A filesystem path resolved outside every allowed root."""


# --- Permissions -----------------------------------------------------------


class PermissionDeniedError(PersonalAIOSError):
    """An action was refused by policy or by the user.

    Named with a suffix so it never shadows the builtin ``PermissionError``.
    """


# --- Agents ----------------------------------------------------------------


class AgentError(PersonalAIOSError):
    """Base class for agent failures."""


class AgentNotFoundError(AgentError):
    """No agent with that name is registered."""


class AgentSpecError(AgentError):
    """An agent manifest is invalid or internally inconsistent."""


class MaxIterationsExceeded(AgentError):
    """The agent loop hit its iteration ceiling without producing an answer."""
