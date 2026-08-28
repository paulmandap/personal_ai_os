"""Finance tools: the boundary where a model's text becomes money."""

from __future__ import annotations

import pytest

from personal_ai_os.core.errors import ToolExecutionError, ToolInputError
from personal_ai_os.memory.finance import FinanceStore, Verdict
from personal_ai_os.permissions.types import PermissionLevel
from personal_ai_os.tools.base import ToolContext
from personal_ai_os.tools.builtin.finance import (
    AddCommitmentTool,
    AddGoalTool,
    AddTransactionTool,
    AffordabilityCheckTool,
    ListAccountsTool,
    ListCommitmentsTool,
    ListGoalsTool,
    ListTransactionsTool,
    SetBalanceTool,
)


def call(tool, args: dict, ctx: ToolContext):
    return tool.run(tool.validate_input(args), ctx)


@pytest.fixture
def finance(store) -> FinanceStore:
    return FinanceStore(store)


class TestPermissionLevels:
    def test_reads_are_read(self):
        for tool in (
            ListAccountsTool(),
            ListTransactionsTool(),
            ListCommitmentsTool(),
            ListGoalsTool(),
            AffordabilityCheckTool(),
        ):
            assert tool.permission is PermissionLevel.READ, tool.name

    def test_writes_are_write_not_spend_money(self):
        """These record facts about money; none of them move any (ADR-014)."""
        for tool in (
            SetBalanceTool(),
            AddTransactionTool(),
            AddCommitmentTool(),
            AddGoalTool(),
        ):
            assert tool.permission is PermissionLevel.WRITE, tool.name


class TestAmountsCrossAsStrings:
    """A float parameter would reintroduce the error the store avoids."""

    def test_a_string_amount_is_accepted(self, tool_context, finance):
        account = call(SetBalanceTool(), {"name": "cash", "amount": "1234.56"}, tool_context)
        assert account.balance_minor == 123_456

    def test_a_comma_formatted_amount_is_accepted(self, tool_context, finance):
        account = call(SetBalanceTool(), {"name": "cash", "amount": "1,234.56"}, tool_context)
        assert account.balance_minor == 123_456

    def test_a_numeric_json_amount_is_coerced_not_floated(self, tool_context, finance):
        """Models emit bare numbers. Pydantic renders to str; the Decimal path
        then parses it, so no binary float is ever constructed."""
        account = call(SetBalanceTool(), {"name": "cash", "amount": 5000}, tool_context)
        assert account.balance_minor == 500_000

    def test_unparseable_amounts_fail_recoverably(self, tool_context, finance):
        with pytest.raises(ToolExecutionError, match="cannot read"):
            call(SetBalanceTool(), {"name": "cash", "amount": "a lot"}, tool_context)


class TestWrites:
    def test_transaction_moves_the_balance(self, tool_context, finance: FinanceStore):
        finance.set_balance("cash", "1000")
        call(
            AddTransactionTool(),
            {"account": "cash", "amount": "-250", "category": "groceries"},
            tool_context,
        )
        assert finance.account("cash").balance_minor == 75_000

    def test_it_returns_the_balance_it_produced(self, tool_context, finance):
        """ADR-032: the tool reports the state it created.

        Without this the closing balance after a spend was a figure no tool
        returned, so the only way for the agent to state one was the
        arithmetic its own prompt forbids.
        """
        finance.set_balance("cash", "3000")
        result = call(
            AddTransactionTool(),
            {"account": "cash", "amount": "-500", "category": "transport"},
            tool_context,
        )
        assert result.account.balance_minor == 250_000
        assert result.transaction.amount_minor == -50_000
        # The rendered figure is what a small model actually reads.
        assert "2,500.00" in result.summary

    def test_the_unknown_account_error_names_the_recovery(self, tool_context, finance):
        """Naming the problem is not enough; the model needs the route back."""
        with pytest.raises(ToolExecutionError, match="set_balance"):
            call(
                AddTransactionTool(),
                {"account": "cash", "amount": "-500"},
                tool_context,
            )

    def test_transaction_on_an_unknown_account_lists_the_known_ones(
        self, tool_context, finance
    ):
        finance.set_balance("cash", "1000")
        with pytest.raises(ToolExecutionError, match="cash"):
            call(
                AddTransactionTool(),
                {"account": "offshore", "amount": "-10"},
                tool_context,
            )

    def test_commitment_day_is_bounded(self, tool_context):
        with pytest.raises(ToolInputError, match="day_of_month"):
            AddCommitmentTool().validate_input(
                {"name": "rent", "amount": "8000", "day_of_month": 45}
            )

    def test_goal_rejects_a_malformed_target_date(self, tool_context, finance):
        with pytest.raises(ToolInputError):
            AddGoalTool().validate_input(
                {"name": "laptop", "target": "60000", "target_date": "next year"}
            )


class TestReads:
    def test_accounts_render_a_summary_for_the_model(self, tool_context, finance):
        finance.set_balance("cash", "1000")
        finance.set_balance("bank", "5000")
        out = call(ListAccountsTool(), {}, tool_context)
        assert out.count == 2
        assert "cash" in out.summary and "bank" in out.summary
        assert out.total == "PHP 6,000.00"

    def test_empty_reads_say_so_rather_than_being_blank(self, tool_context, finance):
        assert call(ListAccountsTool(), {}, tool_context).summary == "(no accounts)"
        assert call(ListGoalsTool(), {}, tool_context).summary == "(no goals)"
        assert call(ListCommitmentsTool(), {}, tool_context).summary == "(no commitments)"


class TestAffordabilityTool:
    def test_it_returns_the_working_not_just_a_verdict(self, tool_context, finance):
        finance.set_balance("bank", "20000")
        finance.add_commitment("rent", "8000", day_of_month=28)
        out = call(AffordabilityCheckTool(), {"amount": "5000"}, tool_context)
        assert out.total_balance_minor == 2_000_000
        assert out.discretionary_minor is not None
        assert out.verdict in set(Verdict)
        assert out.explanation

    def test_an_unaffordable_purchase_is_reported_as_such(self, tool_context, finance):
        finance.set_balance("bank", "100")
        out = call(AffordabilityCheckTool(), {"amount": "5000"}, tool_context)
        assert out.verdict is Verdict.NOT_AFFORDABLE

    def test_the_prompt_shows_the_amount_being_checked(self, tool_context):
        tool = AffordabilityCheckTool()
        args = tool.validate_input({"amount": "5000"})
        assert "5000" in tool.describe_resource(args)

    def test_its_description_forbids_model_arithmetic(self):
        """The instruction has to reach the model, not just the docs."""
        text = AffordabilityCheckTool().description.lower()
        assert "never do the arithmetic" in text


class TestMissingStore:
    def test_tools_report_clearly_when_no_database_was_provided(self, settings):
        bare = ToolContext(workspace_root=settings.workspace_root)
        with pytest.raises(ToolExecutionError, match="not available"):
            call(ListAccountsTool(), {}, bare)


class TestSchemas:
    def test_every_field_carries_a_description(self):
        tools = [
            ListTransactionsTool(),
            AffordabilityCheckTool(),
            SetBalanceTool(),
            AddTransactionTool(),
            AddCommitmentTool(),
            AddGoalTool(),
        ]
        for tool in tools:
            props = tool.schema().parameters.get("properties", {})
            missing = [k for k, v in props.items() if not v.get("description")]
            assert not missing, f"{tool.name}: {missing}"

    def test_the_sign_convention_is_explained_to_the_model(self):
        """Getting the sign wrong reverses a balance change."""
        props = AddTransactionTool().schema().parameters["properties"]
        assert "NEGATIVE" in props["amount"]["description"]
