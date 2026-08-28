"""Finance store: money representation and the arithmetic that decides answers.

These are the highest-stakes tests in the project. Everywhere else, being wrong
produces a bad answer; here it produces a confidently wrong statement about
someone's money.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from personal_ai_os.memory.finance import (
    FinanceError,
    FinanceStore,
    Verdict,
    format_minor,
    to_minor,
)
from personal_ai_os.memory.store import LATEST_VERSION, Store
from personal_ai_os.memory.tasks import TaskStore


@pytest.fixture
def finance(store: Store) -> FinanceStore:
    return FinanceStore(store)


class TestMoneyRepresentation:
    """ADR-023: integer minor units, never floats."""

    @pytest.mark.parametrize(
        ("given", "expected"),
        [
            ("5000", 500_000),
            ("1234.56", 123_456),
            ("1,234.56", 123_456),
            ("0.10", 10),
            ("0.01", 1),
            ("-450.00", -45_000),
            ("₱2,500", 250_000),
            (5000, 500_000),
            (Decimal("19.99"), 1_999),
        ],
    )
    def test_parses_human_amounts(self, given, expected):
        assert to_minor(given) == expected

    def test_a_float_is_refused_outright(self):
        """The one place the representation error could sneak back in."""
        with pytest.raises(FinanceError, match="float"):
            to_minor(1234.56)

    def test_the_classic_float_error_cannot_occur(self):
        """0.1 + 0.2 != 0.3 in binary floating point. In minor units it does."""
        assert to_minor("0.10") + to_minor("0.20") == to_minor("0.30")

    def test_repeated_addition_does_not_drift(self):
        total = sum(to_minor("0.01") for _ in range(1000))
        assert total == to_minor("10.00")

    def test_nonsense_is_rejected_with_a_useful_message(self):
        with pytest.raises(FinanceError, match="cannot read"):
            to_minor("about five thousand")

    def test_formatting_round_trips(self):
        assert format_minor(123_456, "PHP") == "PHP 1,234.56"
        assert format_minor(-45_000, "PHP") == "-PHP 450.00"
        assert format_minor(1, "PHP") == "PHP 0.01"


class TestMigration:
    def test_finance_tables_are_migration_two(self, tmp_path: Path):
        with Store(tmp_path / "f.db") as store:
            assert store.version == LATEST_VERSION >= 2
            FinanceStore(store).set_balance("cash", "100")

    def test_an_existing_v1_database_gains_the_finance_tables(self, tmp_path: Path):
        """Migration 1 already ran on the real database; it must not be edited."""
        path = tmp_path / "f.db"
        with Store(path) as first:
            TaskStore(first).add("pre-existing task")
        with Store(path) as second:
            FinanceStore(second).set_balance("cash", "100")
            assert TaskStore(second).count() == 1  # old data survived


class TestAccounts:
    def test_set_balance_creates_then_updates(self, finance: FinanceStore):
        finance.set_balance("cash", "1000")
        assert finance.account("cash").balance_minor == 100_000
        finance.set_balance("cash", "2500.50")
        assert finance.account("cash").balance_minor == 250_050
        assert len(finance.accounts()) == 1

    def test_total_sums_every_account(self, finance: FinanceStore):
        finance.set_balance("cash", "1000")
        finance.set_balance("bank", "5000")
        assert finance.total_balance_minor() == 600_000

    def test_unknown_account_lists_the_known_ones(self, finance: FinanceStore):
        finance.set_balance("cash", "1")
        with pytest.raises(FinanceError, match="cash"):
            finance.account("offshore")


class TestTransactions:
    def test_spending_reduces_the_balance(self, finance: FinanceStore):
        finance.set_balance("cash", "1000")
        finance.add_transaction("cash", "-250.50", category="groceries")
        assert finance.account("cash").balance_minor == 74_950

    def test_income_increases_the_balance(self, finance: FinanceStore):
        finance.set_balance("cash", "1000")
        finance.add_transaction("cash", "500")
        assert finance.account("cash").balance_minor == 150_000

    def test_ledger_and_balance_move_together(self, finance: FinanceStore):
        """One transaction, or neither -- a ledger that disagrees with the
        balance is worse than no ledger."""
        finance.set_balance("cash", "1000")
        finance.add_transaction("cash", "-100")
        finance.add_transaction("cash", "-200")
        moved = sum(t.amount_minor for t in finance.transactions())
        assert finance.account("cash").balance_minor == 100_000 + moved

    def test_filtering_by_category(self, finance: FinanceStore):
        finance.set_balance("cash", "1000")
        finance.add_transaction("cash", "-100", category="groceries")
        finance.add_transaction("cash", "-50", category="transport")
        assert len(finance.transactions(category="groceries")) == 1


class TestCommitments:
    def test_only_bills_not_yet_due_this_month_count(self, finance: FinanceStore):
        finance.add_commitment("rent", "8000", day_of_month=5)
        finance.add_commitment("internet", "1500", day_of_month=20)
        # On the 10th, rent has already gone out; internet has not.
        due = finance.upcoming_commitments_minor(today=date(2026, 8, 10))
        assert due == 150_000

    def test_on_the_due_day_the_bill_still_counts(self, finance: FinanceStore):
        finance.add_commitment("rent", "8000", day_of_month=5)
        assert finance.upcoming_commitments_minor(today=date(2026, 8, 5)) == 800_000

    def test_a_negative_commitment_is_rejected(self, finance: FinanceStore):
        with pytest.raises(ValueError):
            finance.add_commitment("odd", "-100", day_of_month=1)


class TestGoalReserve:
    def test_a_dated_goal_is_spread_over_the_months_remaining(self, finance):
        """A distant target must not make everything unaffordable today."""
        finance.add_goal("laptop", "60000", target_date="2027-08-01")
        reserved = finance.goal_reserved_minor(today=date(2026, 8, 1))
        assert reserved == 500_000  # 60,000 over 12 months

    def test_an_undated_goal_reserves_nothing(self, finance: FinanceStore):
        """An aspiration is not a commitment; treating it as one would
        quietly block every purchase."""
        finance.add_goal("someday fund", "1000000")
        assert finance.goal_reserved_minor(today=date(2026, 8, 1)) == 0

    def test_progress_already_made_reduces_the_reserve(self, finance: FinanceStore):
        finance.add_goal("laptop", "60000", saved="48000", target_date="2027-08-01")
        assert finance.goal_reserved_minor(today=date(2026, 8, 1)) == 100_000

    def test_a_met_goal_reserves_nothing(self, finance: FinanceStore):
        finance.add_goal("done", "1000", saved="1000", target_date="2027-01-01")
        assert finance.goal_reserved_minor(today=date(2026, 8, 1)) == 0

    def test_a_goal_due_this_month_reserves_the_whole_remainder(self, finance):
        finance.add_goal("deposit", "5000", target_date="2026-08-20")
        assert finance.goal_reserved_minor(today=date(2026, 8, 1)) == 500_000


class TestAccountResolution:
    """Requiring an exact account name is what made a model invent one.

    Observed: asked to transfer from "my savings account", the agent used
    "BPI savings" -- which did not exist -- and the failed leg left the ledger
    inconsistent. Same lesson as ADR-022, with money at stake.
    """

    def test_exact_name_wins(self, finance: FinanceStore):
        finance.set_balance("savings", "100")
        finance.set_balance("BPI savings", "200")
        assert finance.account("savings").balance_minor == 10_000

    def test_a_qualified_name_finds_the_account(self, finance: FinanceStore):
        finance.set_balance("savings", "20000")
        finance.set_balance("cash", "1000")
        assert finance.account("BPI savings").name == "savings"

    def test_ambiguity_is_refused_not_guessed(self, finance: FinanceStore):
        finance.set_balance("BPI savings", "100")
        finance.set_balance("BDO savings", "200")
        with pytest.raises(FinanceError, match="more than one account"):
            finance.account("my savings")

    def test_no_match_lists_what_exists(self, finance: FinanceStore):
        finance.set_balance("cash", "100")
        with pytest.raises(FinanceError, match="cash"):
            finance.account("brokerage")


class TestTransferIsAtomic:
    """ADR-029. The defect this exists to prevent created money."""

    @pytest.fixture
    def two_accounts(self, finance: FinanceStore) -> FinanceStore:
        finance.set_balance("savings", "20000")
        finance.set_balance("cash", "1000")
        return finance

    def test_both_legs_move(self, two_accounts: FinanceStore):
        source, target = two_accounts.transfer("savings", "cash", "5000")
        assert source.balance_minor == 1_500_000
        assert target.balance_minor == 600_000

    def test_the_total_is_unchanged(self, two_accounts: FinanceStore):
        before = two_accounts.total_balance_minor()
        two_accounts.transfer("savings", "cash", "5000")
        assert two_accounts.total_balance_minor() == before

    def test_both_legs_are_recorded_as_transactions(self, two_accounts: FinanceStore):
        two_accounts.transfer("savings", "cash", "5000")
        moves = [t for t in two_accounts.transactions() if t.category == "transfer"]
        assert len(moves) == 2
        assert sum(t.amount_minor for t in moves) == 0

    def test_insufficient_funds_changes_nothing(self, two_accounts: FinanceStore):
        """The failure mode that motivated the tool: no half-applied transfer."""
        with pytest.raises(FinanceError, match="holds only"):
            two_accounts.transfer("cash", "savings", "90000")
        assert two_accounts.account("cash").balance_minor == 100_000
        assert two_accounts.account("savings").balance_minor == 2_000_000
        assert two_accounts.transactions() == []

    def test_an_unknown_account_changes_nothing(self, two_accounts: FinanceStore):
        with pytest.raises(FinanceError):
            two_accounts.transfer("brokerage", "cash", "100")
        assert two_accounts.account("cash").balance_minor == 100_000

    def test_transfer_to_itself_is_refused(self, two_accounts: FinanceStore):
        with pytest.raises(FinanceError, match="itself"):
            two_accounts.transfer("cash", "cash", "100")

    def test_cross_currency_is_refused(self, finance: FinanceStore):
        finance.set_balance("peso", "1000", currency="PHP")
        finance.set_balance("dollar", "1000", currency="USD")
        with pytest.raises(FinanceError, match="exchange rate"):
            finance.transfer("peso", "dollar", "100")

    def test_a_non_positive_amount_is_refused(self, two_accounts: FinanceStore):
        with pytest.raises(FinanceError, match="positive"):
            two_accounts.transfer("savings", "cash", "0")


class TestMixedCurrency:
    def test_a_combined_total_is_refused(self, finance: FinanceStore):
        """Adding PHP to USD is silently, confidently wrong."""
        finance.set_balance("peso", "1000", currency="PHP")
        finance.set_balance("dollar", "100", currency="USD")
        with pytest.raises(FinanceError, match="more than one currency"):
            finance.total_balance_minor()

    def test_the_error_names_both_currencies(self, finance: FinanceStore):
        finance.set_balance("peso", "1000", currency="PHP")
        finance.set_balance("dollar", "100", currency="USD")
        with pytest.raises(FinanceError, match="PHP.*USD"):
            finance.total_balance_minor()

    def test_a_single_currency_still_totals(self, finance: FinanceStore):
        finance.set_balance("a", "1000")
        finance.set_balance("b", "500")
        assert finance.total_balance_minor() == 150_000


class TestAffordability:
    """ADR-024: this arithmetic never happens in a language model."""

    @pytest.fixture
    def setup(self, finance: FinanceStore) -> FinanceStore:
        finance.set_balance("bank", "20000")
        finance.add_commitment("rent", "8000", day_of_month=25)
        finance.add_goal("laptop", "12000", target_date="2027-08-01")  # 1000/mo
        return finance

    def test_the_working_is_shown_not_just_the_answer(self, setup: FinanceStore):
        got = setup.affordability("5000", today=date(2026, 8, 1))
        assert got.total_balance_minor == 2_000_000
        assert got.upcoming_commitments_minor == 800_000
        assert got.goal_reserved_minor == 100_000
        assert got.discretionary_minor == 1_100_000
        assert got.remaining_after_minor == 600_000

    def test_affordable_when_comfortably_within_discretionary(self, setup):
        assert setup.affordability("5000", today=date(2026, 8, 1)).verdict is Verdict.AFFORDABLE

    def test_not_affordable_is_stated_plainly(self, setup: FinanceStore):
        got = setup.affordability("15000", today=date(2026, 8, 1))
        assert got.verdict is Verdict.NOT_AFFORDABLE
        assert got.remaining_after_minor < 0
        assert "Short by" in got.explanation

    def test_tight_is_distinguished_from_affordable(self, setup: FinanceStore):
        """PHP 11,000 discretionary minus 10,800 leaves 200 -- technically
        yes, but calling that 'affordable' would be misleading."""
        got = setup.affordability("10800", today=date(2026, 8, 1))
        assert got.verdict is Verdict.TIGHT

    def test_an_empty_ledger_is_not_affordable(self, finance: FinanceStore):
        got = finance.affordability("100")
        assert got.verdict is Verdict.NOT_AFFORDABLE
        assert got.total_balance_minor == 0

    def test_exact_amounts_do_not_drift(self, finance: FinanceStore):
        finance.set_balance("cash", "1000.00")
        got = finance.affordability("1000.00")
        assert got.remaining_after_minor == 0
        assert got.verdict is Verdict.TIGHT  # zero left is not comfortable

    def test_summary_shows_every_component(self, setup: FinanceStore):
        text = setup.affordability("5000", today=date(2026, 8, 1)).summary()
        for label in ("requested", "total balance", "upcoming bills",
                      "reserved for goals", "discretionary", "verdict"):
            assert label in text
