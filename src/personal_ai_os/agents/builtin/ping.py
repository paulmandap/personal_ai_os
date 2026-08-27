"""The vertical-slice agent.

Its job is to prove the whole path works end to end: registry -> router ->
local model -> tool schema -> permission gate -> tool -> trace.

It also demonstrates the extension point. Most agents will not need a class at
all -- omit ``entrypoint`` from a manifest and you get :class:`BaseAgent`, so
an agent can be pure configuration. Subclass only when an agent needs to shape
its own prompt or post-process its own result, as here.
"""

from __future__ import annotations

from personal_ai_os.agents.base import BaseAgent

PING_SYSTEM_PROMPT = """\
You are the ping agent inside a local personal AI system. You exist to verify \
that tool calling works end to end on this machine.

You can inspect the workspace with the tools you have been given. Use them \
whenever the question is about files or their contents -- never guess at a \
file's contents, and never invent a filename.

The workspace root is: {workspace_root}
Relative paths are resolved from there, and paths outside it will be refused.

Answer concisely. When you have what you need, give the final answer with no \
further tool calls."""


class PingAgent(BaseAgent):
    """A minimal tool-using agent that knows where it is standing."""

    def system_prompt(self) -> str:
        # Injecting the workspace root matters more than it looks: without it
        # a small model tends to guess absolute paths, which the filesystem
        # jail then refuses, burning an iteration on a self-inflicted error.
        base = self._system_prompt or self.spec.system_prompt or PING_SYSTEM_PROMPT
        return base.replace("{workspace_root}", str(self.context.workspace_root))
