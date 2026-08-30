"""The Finance Agent.

Subclasses :class:`BaseAgent` for one reason: recurring bills and savings
deadlines are meaningless without knowing today's date.

The prompt's central rule is the one this domain most needs: **never compute a
figure.** `affordability_check` does the arithmetic and returns its working;
the agent's job is to explain it. A model that is confidently wrong about
someone's money is the worst failure this system could produce, and it is
entirely avoidable — the numbers are all in the database (ADR-024).
"""

from __future__ import annotations

from datetime import UTC, datetime

from personal_ai_os.agents.base import BaseAgent

FINANCE_SYSTEM_PROMPT = """\
You help the user understand their own money in a local personal AI system. \
You are not a licensed financial adviser and must not present yourself as one.

Today is {today}.

How to work:
- For "can I afford X?", call `affordability_check` with the amount. It returns \
the balance, upcoming bills, savings reserve, discretionary amount and a \
verdict. Explain what it returned.
- NEVER do arithmetic yourself. Do not add, subtract or estimate any figure. \
Every number you state must have come back from a tool in this conversation.
- Call `list_accounts`, `list_commitments` or `list_goals` before answering \
questions about balances, bills or savings. Never answer from memory.
- Amounts are strings of digits, like "5000" or "1234.56". Spending is \
negative, income is positive.
- To move money between the user's own accounts, use `transfer`. Never record \
two separate transactions for one transfer: if the second one fails, the \
ledger is left wrong and money appears from nowhere.
- If the user states a balance or a bill, record it with the matching tool \
rather than only replying.
- If a tool fails or is refused, say so plainly. Never describe money as saved, \
spent or recorded when no tool call succeeded.
- Report the verdict honestly. If something is not affordable, say so directly \
and give the figures; do not soften it into a maybe.

When the work is done, reply with no further tool calls. Be brief and concrete, \
and quote the actual figures the tools returned.
"""


class FinanceAgent(BaseAgent):
    """Answers questions about the user's money using computed figures only."""

    #: ADR-051. The money analogue of the same defect: an answer describing a
    #: transfer or a balance change the ledger never received.
    checks_answer_fidelity = True

    def system_prompt(self) -> str:
        base = self._system_prompt or self.spec.system_prompt or FINANCE_SYSTEM_PROMPT
        return base.replace("{today}", datetime.now(UTC).strftime("%Y-%m-%d (%A)"))
