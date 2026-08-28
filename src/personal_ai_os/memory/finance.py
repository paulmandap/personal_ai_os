"""Personal finance: accounts, transactions, commitments, goals.

**Money is integer minor units throughout** -- centavos, not pesos (ADR-023).
Binary floating point cannot represent 0.10, so a float-based ledger drifts,
and a personal finance system that is quietly wrong about money is worse than
one that refuses to run. Conversion happens only at the edges, through
:func:`to_minor` and :func:`format_minor`, and uses ``Decimal`` rather than
``float`` even there.

The other rule that shapes this module: **the arithmetic that decides an answer
lives here, not in the model** (ADR-024). :meth:`FinanceStore.affordability`
returns both the components and the conclusion, so an agent explains a figure
it was given rather than deriving one it might get wrong.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import Enum

from pydantic import BaseModel, Field, field_validator

from personal_ai_os.core.errors import PersonalAIOSError
from personal_ai_os.core.ids import utc_iso
from personal_ai_os.memory.store import Store
from personal_ai_os.memory.tasks import validate_iso_date

MINOR_UNITS = 100  # centavos per peso; same for cents per dollar


class FinanceError(PersonalAIOSError):
    """A finance record was not found, or an amount was unusable."""


# --- money -----------------------------------------------------------------


def to_minor(amount: str | int | Decimal) -> int:
    """Parse a human amount into integer minor units.

    Accepts ``"1,234.56"``, ``"1234.56"``, ``1234`` or a ``Decimal``.
    **Rejects float**, deliberately: accepting one would silently reintroduce
    the representation error this module exists to avoid, at the one place it
    is easiest to miss.
    """
    if isinstance(amount, float):
        raise FinanceError(
            "refusing a float amount; pass a string like '1234.56' or an "
            "integer number of minor units"
        )
    if isinstance(amount, int):
        value = Decimal(amount)
    elif isinstance(amount, Decimal):
        value = amount
    else:
        cleaned = str(amount).replace(",", "").replace("_", "").strip()
        for symbol in ("₱", "$", "PHP", "USD"):
            cleaned = cleaned.replace(symbol, "")
        cleaned = cleaned.strip()
        try:
            value = Decimal(cleaned)
        except (InvalidOperation, ValueError):
            raise FinanceError(
                f"cannot read {amount!r} as an amount; use digits like '1234.56'"
            ) from None

    minor = (value * MINOR_UNITS).to_integral_value()
    return int(minor)


def format_minor(minor: int, currency: str = "PHP") -> str:
    """Render minor units for a human, e.g. ``PHP 1,234.56``."""
    sign = "-" if minor < 0 else ""
    whole, part = divmod(abs(minor), MINOR_UNITS)
    return f"{sign}{currency} {whole:,}.{part:02d}"


# --- records ---------------------------------------------------------------


class Account(BaseModel):
    id: int | None = None
    name: str = Field(min_length=1, max_length=100)
    currency: str = "PHP"
    balance_minor: int = 0
    updated_at: str = ""

    def summary(self) -> str:
        return f"{self.name}: {format_minor(self.balance_minor, self.currency)}"


class Transaction(BaseModel):
    id: int | None = None
    account_id: int
    occurred_on: str
    #: Negative is money out, positive is money in.
    amount_minor: int
    category: str = "uncategorised"
    description: str = ""
    created_at: str = ""

    _check_date = field_validator("occurred_on")(lambda v: validate_iso_date(v) or v)

    def summary(self, currency: str = "PHP") -> str:
        return (
            f"{self.occurred_on}  {format_minor(self.amount_minor, currency):>16}  "
            f"{self.category:<14} {self.description}"
        )


class Commitment(BaseModel):
    """A recurring obligation, e.g. rent on the 5th."""

    id: int | None = None
    name: str = Field(min_length=1, max_length=100)
    #: Positive: the amount owed each month.
    amount_minor: int = Field(gt=0)
    day_of_month: int = Field(ge=1, le=31)
    category: str = "bills"
    active: bool = True

    def summary(self, currency: str = "PHP") -> str:
        state = "" if self.active else "  (inactive)"
        return (
            f"{self.name}: {format_minor(self.amount_minor, currency)} "
            f"on day {self.day_of_month}{state}"
        )


class Goal(BaseModel):
    id: int | None = None
    name: str = Field(min_length=1, max_length=100)
    target_minor: int = Field(gt=0)
    saved_minor: int = 0
    target_date: str | None = None

    _check_date = field_validator("target_date")(lambda v: validate_iso_date(v))

    @property
    def remaining_minor(self) -> int:
        return max(0, self.target_minor - self.saved_minor)

    def summary(self, currency: str = "PHP") -> str:
        by = f" by {self.target_date}" if self.target_date else ""
        return (
            f"{self.name}: {format_minor(self.saved_minor)} of "
            f"{format_minor(self.target_minor, currency)}{by}"
        )


class Verdict(str, Enum):
    AFFORDABLE = "affordable"
    TIGHT = "tight"
    NOT_AFFORDABLE = "not_affordable"


class Affordability(BaseModel):
    """The full working, not just the answer.

    Every figure here is computed in Python. The agent's job is to explain
    this, never to derive it (ADR-024).
    """

    currency: str = "PHP"
    requested_minor: int
    total_balance_minor: int
    upcoming_commitments_minor: int
    goal_reserved_minor: int
    discretionary_minor: int
    remaining_after_minor: int
    verdict: Verdict
    explanation: str

    def summary(self) -> str:
        money = lambda m: format_minor(m, self.currency)  # noqa: E731
        return (
            f"requested            {money(self.requested_minor)}\n"
            f"total balance        {money(self.total_balance_minor)}\n"
            f"upcoming bills      -{money(self.upcoming_commitments_minor)}\n"
            f"reserved for goals  -{money(self.goal_reserved_minor)}\n"
            f"discretionary        {money(self.discretionary_minor)}\n"
            f"after this purchase  {money(self.remaining_after_minor)}\n"
            f"verdict              {self.verdict.value}"
        )


# --- store -----------------------------------------------------------------

#: Below this much left over, "affordable" overstates it.
TIGHT_MARGIN_MINOR = 50_000  # PHP 500.00


class FinanceStore:
    """Typed access to the finance tables."""

    def __init__(self, store: Store) -> None:
        self._store = store

    # --- accounts ----------------------------------------------------------

    def set_balance(
        self, name: str, amount: str | int | Decimal, *, currency: str = "PHP"
    ) -> Account:
        """Create the account or update its balance. Idempotent by name."""
        minor = to_minor(amount)
        now = utc_iso()
        with self._store.write() as conn:
            conn.execute(
                """
                INSERT INTO accounts (name, currency, balance_minor, updated_at)
                     VALUES (?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                     balance_minor = excluded.balance_minor,
                     currency = excluded.currency,
                     updated_at = excluded.updated_at
                """,
                (name, currency, minor, now),
            )
        return self.account(name)

    def account(self, name: str) -> Account:
        row = self._store.query_one("SELECT * FROM accounts WHERE name = ?", (name,))
        if row is None:
            known = [a.name for a in self.accounts()]
            raise FinanceError(f"no account named {name!r}. Known accounts: {known}")
        return Account(**dict(row))

    def accounts(self) -> list[Account]:
        rows = self._store.query("SELECT * FROM accounts ORDER BY name")
        return [Account(**dict(r)) for r in rows]

    def total_balance_minor(self) -> int:
        return sum(a.balance_minor for a in self.accounts())

    # --- transactions ------------------------------------------------------

    def add_transaction(
        self,
        account_name: str,
        amount: str | int | Decimal,
        *,
        occurred_on: str | None = None,
        category: str = "uncategorised",
        description: str = "",
    ) -> Transaction:
        """Record a transaction and move the account balance by the same amount.

        Both writes happen in one transaction: a ledger where the entry landed
        but the balance did not is worse than no ledger.
        """
        account = self.account(account_name)
        assert account.id is not None
        minor = to_minor(amount)
        when = occurred_on or date.today().isoformat()
        record = Transaction(
            account_id=account.id,
            occurred_on=when,
            amount_minor=minor,
            category=category,
            description=description,
        )

        with self._store.write() as conn:
            cursor = conn.execute(
                """
                INSERT INTO transactions
                    (account_id, occurred_on, amount_minor, category, description,
                     created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    record.account_id,
                    record.occurred_on,
                    record.amount_minor,
                    record.category,
                    record.description,
                    utc_iso(),
                ),
            )
            conn.execute(
                "UPDATE accounts SET balance_minor = balance_minor + ?, "
                "updated_at = ? WHERE id = ?",
                (minor, utc_iso(), account.id),
            )
            record.id = int(cursor.lastrowid or 0)
        return record

    def transactions(self, *, limit: int = 50, category: str | None = None) -> list[Transaction]:
        if category:
            rows = self._store.query(
                "SELECT * FROM transactions WHERE category = ? "
                "ORDER BY occurred_on DESC, id DESC LIMIT ?",
                (category, limit),
            )
        else:
            rows = self._store.query(
                "SELECT * FROM transactions ORDER BY occurred_on DESC, id DESC LIMIT ?",
                (limit,),
            )
        return [Transaction(**dict(r)) for r in rows]

    # --- commitments -------------------------------------------------------

    def add_commitment(
        self,
        name: str,
        amount: str | int | Decimal,
        day_of_month: int,
        *,
        category: str = "bills",
    ) -> Commitment:
        record = Commitment(
            name=name,
            amount_minor=to_minor(amount),
            day_of_month=day_of_month,
            category=category,
        )
        with self._store.write() as conn:
            cursor = conn.execute(
                "INSERT INTO commitments (name, amount_minor, day_of_month, "
                "category, active) VALUES (?, ?, ?, ?, 1)",
                (record.name, record.amount_minor, record.day_of_month, record.category),
            )
            record.id = int(cursor.lastrowid or 0)
        return record

    def commitments(self, *, active_only: bool = True) -> list[Commitment]:
        sql = "SELECT * FROM commitments"
        if active_only:
            sql += " WHERE active = 1"
        sql += " ORDER BY day_of_month, name"
        return [Commitment(**dict(r)) for r in self._store.query(sql)]

    def upcoming_commitments_minor(self, *, today: date | None = None) -> int:
        """What is still owed this month, counting from today inclusive."""
        day = (today or date.today()).day
        return sum(
            c.amount_minor for c in self.commitments() if c.day_of_month >= day
        )

    # --- goals -------------------------------------------------------------

    def add_goal(
        self,
        name: str,
        target: str | int | Decimal,
        *,
        saved: str | int | Decimal = 0,
        target_date: str | None = None,
    ) -> Goal:
        record = Goal(
            name=name,
            target_minor=to_minor(target),
            saved_minor=to_minor(saved),
            target_date=target_date,
        )
        with self._store.write() as conn:
            cursor = conn.execute(
                "INSERT INTO goals (name, target_minor, saved_minor, target_date) "
                "VALUES (?, ?, ?, ?)",
                (record.name, record.target_minor, record.saved_minor, record.target_date),
            )
            record.id = int(cursor.lastrowid or 0)
        return record

    def goals(self) -> list[Goal]:
        rows = self._store.query("SELECT * FROM goals ORDER BY target_date, name")
        return [Goal(**dict(r)) for r in rows]

    def goal_reserved_minor(self, *, today: date | None = None) -> int:
        """This month's share of what still needs saving.

        A dated goal is spread evenly over the months remaining, so a distant
        target does not make everything look unaffordable today. An undated
        goal reserves nothing -- it is an aspiration, not a commitment, and
        treating it as one would quietly block every purchase.
        """
        now = today or date.today()
        reserved = 0
        for goal in self.goals():
            remaining = goal.remaining_minor
            if not remaining or not goal.target_date:
                continue
            target = date.fromisoformat(goal.target_date)
            months = max(
                1, (target.year - now.year) * 12 + (target.month - now.month)
            )
            reserved += -(-remaining // months)  # ceiling division
        return reserved

    # --- the decision ------------------------------------------------------

    def affordability(
        self, amount: str | int | Decimal, *, today: date | None = None
    ) -> Affordability:
        """Compute whether a purchase fits, and show the working.

        This is the arithmetic a language model must never do (ADR-024): the
        numbers are all here, and getting one wrong means being confidently
        wrong about someone's money.
        """
        requested = to_minor(amount)
        accounts = self.accounts()
        currency = accounts[0].currency if accounts else "PHP"

        balance = self.total_balance_minor()
        upcoming = self.upcoming_commitments_minor(today=today)
        reserved = self.goal_reserved_minor(today=today)
        discretionary = balance - upcoming - reserved
        remaining = discretionary - requested

        if remaining < 0:
            verdict = Verdict.NOT_AFFORDABLE
            explanation = (
                f"Short by {format_minor(-remaining, currency)} after covering "
                f"bills and savings."
            )
        elif remaining < TIGHT_MARGIN_MINOR:
            verdict = Verdict.TIGHT
            explanation = (
                f"Possible, but only {format_minor(remaining, currency)} would "
                f"be left over."
            )
        else:
            verdict = Verdict.AFFORDABLE
            explanation = (
                f"{format_minor(remaining, currency)} would remain after bills "
                f"and savings."
            )

        return Affordability(
            currency=currency,
            requested_minor=requested,
            total_balance_minor=balance,
            upcoming_commitments_minor=upcoming,
            goal_reserved_minor=reserved,
            discretionary_minor=discretionary,
            remaining_after_minor=remaining,
            verdict=verdict,
            explanation=explanation,
        )
