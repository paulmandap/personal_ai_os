"""Finance tools.

Two rules run through all of them.

**Amounts cross this boundary as strings**, never floats. The model writes
``"5000"`` or ``"1,234.56"``; :func:`to_minor` parses it with ``Decimal`` and
stores integer minor units (ADR-023). A float parameter would silently
reintroduce the representation error the storage layer exists to avoid.

**The arithmetic that decides an answer is in the tool** (ADR-024).
``affordability_check`` returns the balance, the bills, the savings reserve,
the discretionary figure *and* the verdict. The agent explains that; it never
computes it. A language model doing the subtraction that decides whether
someone can afford something is a defect waiting to happen, and it is entirely
avoidable -- the numbers are all in a database.

These are ``write``-level, not ``spend_money``: they record facts *about*
money. Nothing here moves any. Mislabelling would train click-through on the
prompt that should matter most (ADR-014).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

from personal_ai_os.core.errors import ToolExecutionError
from personal_ai_os.memory.finance import (
    Account,
    Affordability,
    Commitment,
    FinanceError,
    FinanceStore,
    Goal,
    Transaction,
    format_minor,
)
from personal_ai_os.memory.tasks import validate_iso_date
from personal_ai_os.permissions.types import PermissionLevel
from personal_ai_os.tools.base import Tool, ToolContext, ToolInput

AMOUNT_HELP = (
    "Amount as digits in a string, e.g. '5000' or '1234.56'. Do not include "
    "currency symbols or words."
)


def _as_amount_string(value: Any) -> Any:
    """Accept what a model actually emits, without a float ever reaching storage.

    Models emit bare JSON numbers for amounts constantly, and JSON `1234.56`
    arrives in Python as a float. Rejecting it would be pedantic; carrying it
    inward would defeat ADR-023. So the boundary is tolerant and the core stays
    strict: stringify here â€” `str(1234.56)` is `'1234.56'`, Python's shortest
    round-trip form â€” and let `to_minor` parse it with Decimal.
    """
    if isinstance(value, bool):  # bool is an int; nobody means a boolean amount
        raise ValueError("amount must be a number or a string of digits")
    if isinstance(value, (int, float)):
        return str(value)
    return value


def _amount_field(*names: str):
    """Attach the tolerant coercion to each amount field on a model."""
    return field_validator(*names, mode="before")(lambda v: _as_amount_string(v))


def _finance(ctx: ToolContext) -> FinanceStore:
    if ctx.store is None:
        raise ToolExecutionError(
            "financial storage is not available in this context; the runtime "
            "did not provide a database"
        )
    return FinanceStore(ctx.store)


# --- reads -----------------------------------------------------------------


class NoInput(ToolInput):
    pass


class AccountsOutput(BaseModel):
    count: int
    accounts: list[Account]
    total: str
    summary: str


class ListAccountsTool(Tool):
    name = "list_accounts"
    description = (
        "List the user's accounts and balances. Call this before answering any "
        "question about how much money they have."
    )
    Input = NoInput
    Output = AccountsOutput
    permission = PermissionLevel.READ
    timeout_s = 10.0

    def run(self, args: NoInput, ctx: ToolContext) -> AccountsOutput:  # type: ignore[override]
        store = _finance(ctx)
        accounts = store.accounts()
        currency = accounts[0].currency if accounts else "PHP"
        try:
            total = format_minor(store.total_balance_minor(), currency)
        except FinanceError as exc:
            # Mixed currencies: report the accounts, decline the total. Better
            # a partial answer than a confidently wrong number.
            total = f"unavailable - {exc}"
        return AccountsOutput(
            count=len(accounts),
            accounts=accounts,
            total=total,
            summary="\n".join(a.summary for a in accounts) or "(no accounts)",
        )


class ListTransactionsInput(ToolInput):
    limit: int = Field(
        default=20, ge=1, le=200, description="Maximum transactions to return."
    )
    category: str | None = Field(
        default=None, description="Optional category filter, e.g. 'groceries'."
    )


class TransactionsOutput(BaseModel):
    count: int
    transactions: list[Transaction]
    summary: str


class ListTransactionsTool(Tool):
    name = "list_transactions"
    description = "List recent transactions, newest first. Negative amounts are money out."
    Input = ListTransactionsInput
    Output = TransactionsOutput
    permission = PermissionLevel.READ
    timeout_s = 10.0

    def run(self, args: ListTransactionsInput, ctx: ToolContext) -> TransactionsOutput:  # type: ignore[override]
        found = _finance(ctx).transactions(limit=args.limit, category=args.category)
        return TransactionsOutput(
            count=len(found),
            transactions=found,
            summary="\n".join(t.summary for t in found) or "(no transactions)",
        )


class CommitmentsOutput(BaseModel):
    count: int
    commitments: list[Commitment]
    summary: str


class ListCommitmentsTool(Tool):
    name = "list_commitments"
    description = (
        "List recurring monthly obligations such as rent, tuition or "
        "subscriptions. These are owed regardless of what else is planned."
    )
    Input = NoInput
    Output = CommitmentsOutput
    permission = PermissionLevel.READ
    timeout_s = 10.0

    def run(self, args: NoInput, ctx: ToolContext) -> CommitmentsOutput:  # type: ignore[override]
        found = _finance(ctx).commitments()
        return CommitmentsOutput(
            count=len(found),
            commitments=found,
            summary="\n".join(c.summary for c in found) or "(no commitments)",
        )


class GoalsOutput(BaseModel):
    count: int
    goals: list[Goal]
    summary: str


class ListGoalsTool(Tool):
    name = "list_goals"
    description = "List savings goals and progress towards them."
    Input = NoInput
    Output = GoalsOutput
    permission = PermissionLevel.READ
    timeout_s = 10.0

    def run(self, args: NoInput, ctx: ToolContext) -> GoalsOutput:  # type: ignore[override]
        found = _finance(ctx).goals()
        return GoalsOutput(
            count=len(found),
            goals=found,
            summary="\n".join(g.summary for g in found) or "(no goals)",
        )


# --- the decision ----------------------------------------------------------


class AffordabilityInput(ToolInput):
    amount: str = Field(description=AMOUNT_HELP)

    _coerce = _amount_field("amount")


class AffordabilityCheckTool(Tool):
    name = "affordability_check"
    description = (
        "Work out whether the user can afford a purchase. Returns their "
        "balance, upcoming bills, savings reserve, the discretionary amount "
        "and a verdict -- all calculated for you. "
        "ALWAYS use this for affordability questions. Never do the arithmetic "
        "yourself, and never state a figure this tool did not return."
    )
    Input = AffordabilityInput
    Output = Affordability
    permission = PermissionLevel.READ
    timeout_s = 10.0

    def describe_resource(self, args: AffordabilityInput) -> str:  # type: ignore[override]
        return args.amount

    def run(self, args: AffordabilityInput, ctx: ToolContext) -> Affordability:  # type: ignore[override]
        try:
            return _finance(ctx).affordability(args.amount)
        except FinanceError as exc:
            raise ToolExecutionError(str(exc)) from exc


# --- writes ----------------------------------------------------------------


class SetBalanceInput(ToolInput):
    name: str = Field(description="Account name, e.g. 'BPI savings' or 'cash'.")
    amount: str = Field(description=AMOUNT_HELP)
    currency: str = Field(default="PHP", description="ISO code, e.g. PHP or USD.")

    _coerce = _amount_field("amount")


class SetBalanceTool(Tool):
    name = "set_balance"
    description = (
        "Set an account's balance to a known figure, creating the account if "
        "it does not exist. Use when the user states a current balance."
    )
    Input = SetBalanceInput
    Output = Account
    permission = PermissionLevel.WRITE
    timeout_s = 10.0

    def describe_resource(self, args: SetBalanceInput) -> str:  # type: ignore[override]
        return f"{args.name} = {args.amount}"

    def run(self, args: SetBalanceInput, ctx: ToolContext) -> Account:  # type: ignore[override]
        try:
            return _finance(ctx).set_balance(
                args.name, args.amount, currency=args.currency
            )
        except FinanceError as exc:
            raise ToolExecutionError(str(exc)) from exc


class AddTransactionInput(ToolInput):
    account: str = Field(description="Account name the money moved in or out of.")
    amount: str = Field(
        description=(
            "Signed amount as a string. NEGATIVE for spending ('-450.00'), "
            "positive for income ('15000'). Getting the sign wrong reverses "
            "the balance change."
        )
    )
    category: str = Field(default="uncategorised", description="e.g. groceries, transport.")
    description: str = Field(default="", description="Short note about the transaction.")
    occurred_on: str | None = Field(
        default=None, description="ISO date; defaults to today."
    )

    _coerce = _amount_field("amount")
    # Validate the date at the schema boundary, not inside run(), so a
    # malformed one reaches the model as the message written to help it retry.
    _check_date = field_validator("occurred_on")(lambda v: validate_iso_date(v))


class AddTransactionOutput(BaseModel):
    """The record, and the balance it produced.

    Returning the resulting account is the point (ADR-032). The prompt tells the
    agent every figure it states must have come from a tool -- and before this,
    the closing balance after a spend was a figure no tool returned, so the only
    way to answer was to do the arithmetic the prompt forbids.
    """

    transaction: Transaction
    account: Account
    summary: str


class AddTransactionTool(Tool):
    name = "add_transaction"
    # Says what the tool does and what it returns -- and deliberately does NOT
    # tell the model to call set_balance first. That sentence was here for one
    # measurement and cost 3 of 5 transfer runs on qwen2.5:3b: it made
    # set_balance salient on every turn, and the 3B started assembling a
    # transfer out of two set_balance calls, zeroing an account (ADR-029 is
    # exactly this failure). Guidance for a failure belongs in the refusal,
    # where only the agent that hit it sees it -- not in a description every
    # turn reads.
    description = (
        "Record a transaction and adjust the account balance by the same "
        "amount. Use negative amounts for spending. Returns the account's new "
        "balance -- quote that figure rather than working one out."
    )
    Input = AddTransactionInput
    Output = AddTransactionOutput
    permission = PermissionLevel.WRITE
    timeout_s = 10.0

    def describe_resource(self, args: AddTransactionInput) -> str:  # type: ignore[override]
        return f"{args.amount} on {args.account}"

    def run(  # type: ignore[override]
        self, args: AddTransactionInput, ctx: ToolContext
    ) -> AddTransactionOutput:
        try:
            record, account = _finance(ctx).add_transaction(
                args.account,
                args.amount,
                occurred_on=args.occurred_on,
                category=args.category,
                description=args.description,
            )
        except FinanceError as exc:
            raise ToolExecutionError(str(exc)) from exc

        return AddTransactionOutput(
            transaction=record,
            account=account,
            summary=(
                f"recorded {format_minor(record.amount_minor, account.currency)} "
                f"({record.category}); {account.name} is now "
                f"{format_minor(account.balance_minor, account.currency)}"
            ),
        )


class TransferInput(ToolInput):
    from_account: str = Field(description="Account the money leaves.")
    to_account: str = Field(description="Account the money arrives in.")
    amount: str = Field(description=AMOUNT_HELP + " Always positive.")

    _coerce = _amount_field("amount")


class TransferOutput(BaseModel):
    from_account: Account
    to_account: Account
    summary: str


class TransferTool(Tool):
    name = "transfer"
    description = (
        "Move money between two of the user's own accounts. Both sides happen "
        "together or neither does. ALWAYS use this for a transfer -- never "
        "record two separate transactions, which can leave the ledger wrong if "
        "one of them fails."
    )
    Input = TransferInput
    Output = TransferOutput
    permission = PermissionLevel.WRITE
    timeout_s = 10.0

    def describe_resource(self, args: TransferInput) -> str:  # type: ignore[override]
        return f"{args.amount} from {args.from_account} to {args.to_account}"

    def run(self, args: TransferInput, ctx: ToolContext) -> TransferOutput:  # type: ignore[override]
        try:
            source, target = _finance(ctx).transfer(
                args.from_account, args.to_account, args.amount
            )
        except FinanceError as exc:
            raise ToolExecutionError(str(exc)) from exc
        # "already applied" is the load-bearing phrase. Without it the agent
        # read these post-transfer balances as opening ones, subtracted the
        # amount a second time, and reported 10,000 where the ledger correctly
        # said 15,000 -- in 5 of 5 runs (ADR-033).
        return TransferOutput(
            from_account=source,
            to_account=target,
            summary=(
                f"transfer already applied. Balances now: "
                f"{source.summary}; {target.summary}"
            ),
        )


class AddCommitmentInput(ToolInput):
    name: str = Field(description="What the bill is, e.g. 'rent' or 'tuition'.")
    amount: str = Field(description=AMOUNT_HELP + " Always positive.")
    day_of_month: int = Field(ge=1, le=31, description="Day it falls due, 1-31.")
    category: str = Field(default="bills", description="e.g. bills, tuition, rent.")

    _coerce = _amount_field("amount")


class AddCommitmentTool(Tool):
    name = "add_commitment"
    description = "Record a recurring monthly obligation such as rent or a subscription."
    Input = AddCommitmentInput
    Output = Commitment
    permission = PermissionLevel.WRITE
    timeout_s = 10.0

    def describe_resource(self, args: AddCommitmentInput) -> str:  # type: ignore[override]
        return f"{args.name} {args.amount}/month"

    def run(self, args: AddCommitmentInput, ctx: ToolContext) -> Commitment:  # type: ignore[override]
        try:
            return _finance(ctx).add_commitment(
                args.name, args.amount, args.day_of_month, category=args.category
            )
        except FinanceError as exc:
            raise ToolExecutionError(str(exc)) from exc


class AddGoalInput(ToolInput):
    name: str = Field(description="What the user is saving for.")
    target: str = Field(description="Target amount. " + AMOUNT_HELP)
    saved: str = Field(default="0", description="Amount already saved.")
    target_date: str | None = Field(
        default=None,
        description="Optional ISO date to reach it by, e.g. '2027-06-01'.",
    )

    _coerce = _amount_field("target", "saved")
    _check_date = field_validator("target_date")(lambda v: validate_iso_date(v))


class AddGoalTool(Tool):
    name = "add_goal"
    description = (
        "Record a savings goal. A goal with a target date reserves a share of "
        "each month's money; one without reserves nothing."
    )
    Input = AddGoalInput
    Output = Goal
    permission = PermissionLevel.WRITE
    timeout_s = 10.0

    def describe_resource(self, args: AddGoalInput) -> str:  # type: ignore[override]
        return f"{args.name} target {args.target}"

    def run(self, args: AddGoalInput, ctx: ToolContext) -> Goal:  # type: ignore[override]
        try:
            return _finance(ctx).add_goal(
                args.name,
                args.target,
                saved=args.saved,
                target_date=args.target_date,
            )
        except FinanceError as exc:
            raise ToolExecutionError(str(exc)) from exc
