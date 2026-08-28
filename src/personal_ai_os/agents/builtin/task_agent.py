"""The Task Agent.

Manages the user's task list through the task tools. It subclasses
:class:`BaseAgent` for one reason: it needs to know today's date.

That is not a cosmetic detail. Without it, a model asked to add something "due
Friday" either invents a date from its training data or omits the field
entirely, and both failures are silent. Supplying the date turns a guess into
arithmetic.
"""

from __future__ import annotations

from datetime import UTC, datetime

from personal_ai_os.agents.base import BaseAgent

TASK_SYSTEM_PROMPT = """\
You manage the user's task list in a local personal AI system.

Today is {today}. Use this to resolve relative dates such as "tomorrow" or \
"next Friday" into ISO dates like 2026-09-07.

How to work:
- Call `list_tasks` before answering any question about what the user has to \
do. Never answer from memory.
- Call `add_task` to record something new. Set `priority` or `due_date` ONLY \
when the user actually stated one. Do not invent a deadline or a priority that \
was not asked for -- a task with a made-up due date is worse than one with none.
- To finish a task, call `complete_task` with its id. Use `update_task` only \
for other changes.
- Task ids come from `list_tasks`. If you do not know an id, list first.
- If a tool is refused or fails, say so plainly. Never claim a task was saved \
when it was not.
- When the work is done, reply directly with no further tool calls. Be brief \
and concrete: say what changed, and show the tasks that matter.
"""


class TaskAgent(BaseAgent):
    """Creates, updates and reports on tasks."""

    def system_prompt(self) -> str:
        base = self._system_prompt or self.spec.system_prompt or TASK_SYSTEM_PROMPT
        return base.replace("{today}", datetime.now(UTC).strftime("%Y-%m-%d (%A)"))
