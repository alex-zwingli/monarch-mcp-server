"""Tests for budget-related MCP tools."""

import json

import pytest

from monarch_mcp_server.tools import budgets as budgets_module
from monarch_mcp_server.tools.budgets import get_budgets, set_flexible_budget

FLEX_BLOCK = {
    "budgetVariability": "flexible",
    "monthlyAmounts": [
        {
            "month": "2026-03-01",
            "plannedCashFlowAmount": 2000.00,
            "actualAmount": 1250.00,
            "remainingAmount": 750.00,
            "previousMonthRolloverAmount": 0.00,
            "rolloverType": "monthly",
        }
    ],
}

TOTALS_BLOCK = [
    {
        "month": "2026-03-01",
        "totalFlexibleExpenses": {
            "plannedAmount": 2000.00,
            "actualAmount": 1250.00,
            "remainingAmount": 750.00,
            "previousMonthRolloverAmount": 0.00,
        },
        "totalFixedExpenses": {
            "plannedAmount": 1500.00,
            "actualAmount": 1500.00,
            "remainingAmount": 0.00,
            "previousMonthRolloverAmount": 0.00,
        },
        "totalNonMonthlyExpenses": {
            "plannedAmount": 300.00,
            "actualAmount": 100.00,
            "remainingAmount": 200.00,
            "previousMonthRolloverAmount": 0.00,
        },
    }
]


def with_flex(base_response):
    """Copy the default fixture response, adding flex + totals selections."""
    enriched = json.loads(json.dumps(base_response))
    enriched["budgetData"]["monthlyAmountsForFlexExpense"] = FLEX_BLOCK
    enriched["budgetData"]["totalsByMonth"] = TOTALS_BLOCK
    return enriched


def reject_flex_only(base_response):
    """side_effect that rejects the flex query but serves the narrow one."""

    def _side_effect(*args, **kwargs):
        if kwargs.get("operation") == "MCPBudgetDataFlex":
            raise Exception(
                "Cannot query field 'monthlyAmountsForFlexExpense' on type 'BudgetData'."
            )
        return base_response

    return _side_effect


class TestGetBudgets:
    async def test_returns_formatted_category_rows(self):
        result = json.loads(await get_budgets())
        assert result["tool"] == "get_budgets"
        rows = result["data"]
        assert len(rows) == 2
        groceries = next(row for row in rows if row["id"] == "cat-1")
        assert groceries == {
            "id": "cat-1",
            "name": "Groceries",
            "planned": 500.00,
            "actual": 320.00,
            "remaining": 180.00,
            "category_group": "Food",
            "month": "2026-03-01",
        }

    async def test_passes_explicit_date_params(self, mock_monarch_client):
        await get_budgets(start_date="2026-03-01", end_date="2026-03-31")
        _, kwargs = mock_monarch_client.gql_call.call_args
        assert kwargs["variables"] == {
            "startDate": "2026-03-01",
            "endDate": "2026-03-31",
        }

    async def test_defaults_to_current_month(self, mock_monarch_client):
        from monarch_mcp_server.tools.budgets import current_month_range

        start, end = current_month_range()
        await get_budgets()
        _, kwargs = mock_monarch_client.gql_call.call_args
        assert kwargs["variables"] == {"startDate": start, "endDate": end}

    async def test_handles_api_error(self, mock_monarch_client):
        mock_monarch_client.gql_call.side_effect = Exception("Budget error")
        result = await get_budgets()
        assert "get_budgets" in result


class TestFlexBucket:
    async def test_reports_flex_bucket_when_present(self, mock_monarch_client):
        mock_monarch_client.gql_call.return_value = with_flex(
            mock_monarch_client.gql_call.return_value
        )

        result = json.loads(await get_budgets())

        assert result["flex"] == {
            "status": "ok",
            "budget_variability": "flexible",
            "monthly": [
                {
                    "month": "2026-03-01",
                    "planned": 2000.00,
                    "actual": 1250.00,
                    "remaining": 750.00,
                    "rollover": 0.00,
                    "rollover_type": "monthly",
                }
            ],
        }
        assert result["totals"][0]["flexible"]["planned"] == 2000.00
        assert result["totals"][0]["fixed"]["remaining"] == 0.00
        assert result["totals"][0]["non_monthly"]["actual"] == 100.00

    async def test_prefers_the_flex_query(self, mock_monarch_client):
        await get_budgets()
        _, kwargs = mock_monarch_client.gql_call.call_args
        assert kwargs["operation"] == "MCPBudgetDataFlex"

    async def test_not_configured_when_account_has_no_flex_bucket(self):
        # The default fixture response omits the flex selections entirely.
        result = json.loads(await get_budgets())
        assert result["flex"]["status"] == "not_configured"
        assert result["flex"]["monthly"] == []
        assert result["totals"] is None
        # Category rows are unaffected.
        assert len(result["data"]) == 2

    async def test_falls_back_to_narrow_query_when_flex_rejected(
        self, mock_monarch_client
    ):
        base = mock_monarch_client.gql_call.return_value
        mock_monarch_client.gql_call.side_effect = reject_flex_only(base)

        result = json.loads(await get_budgets())

        assert result["flex"]["status"] == "unsupported"
        assert result["totals"] is None
        # The whole tool still works -- this is the no-regression guarantee.
        assert len(result["data"]) == 2
        operations = [
            call.kwargs["operation"]
            for call in mock_monarch_client.gql_call.call_args_list
        ]
        assert operations == ["MCPBudgetDataFlex", "MCPBudgetData"]

    async def test_caches_rejection_and_skips_retrying_flex(
        self, mock_monarch_client
    ):
        base = mock_monarch_client.gql_call.return_value
        mock_monarch_client.gql_call.side_effect = reject_flex_only(base)

        await get_budgets()
        await get_budgets()

        operations = [
            call.kwargs["operation"]
            for call in mock_monarch_client.gql_call.call_args_list
        ]
        # First call probes then falls back; the second goes straight to narrow.
        assert operations == [
            "MCPBudgetDataFlex",
            "MCPBudgetData",
            "MCPBudgetData",
        ]

    async def test_auth_error_propagates_and_does_not_disable_flex(
        self, mock_monarch_client
    ):
        mock_monarch_client.gql_call.side_effect = Exception(
            "401 Unauthorized: session expired"
        )

        result = json.loads(await get_budgets())

        # Surfaced as an error rather than silently degraded to a partial answer.
        assert result["error"] is True
        assert result["tool"] == "get_budgets"
        # A transport/auth failure must not latch flex off for the process.
        assert budgets_module._flex_supported is None

    @pytest.mark.parametrize(
        "message,expected",
        [
            ("Cannot query field 'totalsByMonth'", True),
            ("Unknown field monthlyAmountsForFlexExpense", True),
            ("401 Unauthorized", False),
            ("Connection reset by peer", False),
            ("", False),
        ],
    )
    def test_schema_rejection_detection(self, message, expected):
        assert (
            budgets_module._is_schema_rejection(Exception(message)) is expected
        )


class TestSetFlexibleBudget:
    async def test_sets_amount(self, mock_monarch_client):
        mock_monarch_client.update_flexible_budget.return_value = {
            "updateOrCreateFlexBudgetItem": {"budgetItem": {"id": "flex-1"}}
        }

        result = json.loads(await set_flexible_budget(amount=2000, apply_to_future=True))

        assert result["success"] is True
        mock_monarch_client.update_flexible_budget.assert_awaited_once_with(
            amount=2000, start_date=None, apply_to_future=True
        )

    async def test_refuses_when_flex_known_unsupported(self, mock_monarch_client):
        budgets_module._flex_supported = False

        result = json.loads(await set_flexible_budget(amount=100))

        assert result["success"] is False
        assert "flexible budget" in result["error"].lower()
        mock_monarch_client.update_flexible_budget.assert_not_awaited()

    async def test_reports_missing_client_method(self, mock_monarch_client):
        del mock_monarch_client.update_flexible_budget

        result = json.loads(await set_flexible_budget(amount=100))

        assert result["success"] is False
        assert "update_flexible_budget" in result["error"]

    async def test_handles_api_error(self, mock_monarch_client):
        mock_monarch_client.update_flexible_budget.side_effect = Exception("boom")
        result = json.loads(await set_flexible_budget(amount=100))
        assert result["error"] is True
        assert result["tool"] == "set_flexible_budget"
