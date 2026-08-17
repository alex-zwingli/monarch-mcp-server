from monarch_mcp_server.tools.budgets import (
    BUDGET_QUERY,
    BUDGET_QUERY_FLEX,
    format_budget_data,
    format_budget_totals,
    format_flex_budget,
)


def query_text(query):
    # gql 4.0 returns a GraphQLRequest wrapping the parsed DocumentNode;
    # the source string lives on document.loc.source.body. Earlier gql 3.x
    # exposed .loc directly on the gql() return value.
    return query.document.loc.source.body


def category_groups_selection(text):
    """The categoryGroups block -- the subtree implicated in the #15 failure."""
    return text[text.index("categoryGroups {"):]


def test_budget_query_avoids_stale_category_group_fields():
    text = query_text(BUDGET_QUERY)

    assert "budgetVariability" not in text
    assert "rolloverPeriod" not in text


def test_fallback_query_requests_nothing_beyond_the_proven_set():
    # The narrow query is the safety net: it must stay exactly the document
    # already known to work, so none of the extended fields may leak into it.
    text = query_text(BUDGET_QUERY)

    for field in (
        "monthlyAmountsForFlexExpense",
        "monthlyAmountsByCategoryGroup",
        "totalsByMonth",
        "previousMonthRolloverAmount",
        "rolloverType",
        "budgetSystem",
    ):
        assert field not in text, f"{field} leaked into the fallback query"


def test_flex_query_keeps_category_groups_narrow():
    text = query_text(BUDGET_QUERY_FLEX)

    # The flex query adds roll-up fields under budgetData...
    assert "monthlyAmountsForFlexExpense" in text
    assert "monthlyAmountsByCategoryGroup" in text
    assert "totalsByMonth" in text
    assert "totalIncome" in text

    # ...but must not reintroduce the categoryGroups fields Monarch rejects.
    # budgetVariability legitimately appears under monthlyAmountsForFlexExpense,
    # so check the categoryGroups subtree specifically rather than the whole doc.
    assert "rolloverPeriod" not in text
    assert "groupLevelBudgetingEnabled" not in text
    assert "budgetVariability" not in category_groups_selection(text)


def test_format_budget_data_returns_current_month_category_rows():
    raw_budget_data = {
        "budgetData": {
            "monthlyAmountsByCategory": [
                {
                    "category": {"id": "cat-1"},
                    "monthlyAmounts": [
                        {
                            "month": "2026-06-01",
                            "plannedCashFlowAmount": -100,
                            "plannedSetAsideAmount": 0,
                            "actualAmount": -25,
                            "remainingAmount": -75,
                        }
                    ],
                }
            ]
        },
        "categoryGroups": [
            {
                "name": "Food",
                "categories": [{"id": "cat-1", "name": "Groceries"}],
            }
        ],
    }

    assert format_budget_data(raw_budget_data) == [
        {
            "id": "cat-1",
            "name": "Groceries",
            "planned": -100,
            "actual": -25,
            "remaining": -75,
            "set_aside": 0,
            "rollover": None,
            "rollover_type": None,
            "category_group": "Food",
            "month": "2026-06-01",
        }
    ]


class TestFormatFlexBudget:
    def test_unsupported_when_fallback_query_was_used(self):
        assert format_flex_budget({"budgetData": {}}, used_flex_query=False) == {
            "status": "unsupported",
            "budget_variability": None,
            "monthly": [],
        }

    def test_not_configured_when_field_absent(self):
        assert (
            format_flex_budget({"budgetData": {}}, used_flex_query=True)["status"]
            == "not_configured"
        )

    def test_not_configured_when_field_is_null(self):
        raw = {"budgetData": {"monthlyAmountsForFlexExpense": None}}
        assert format_flex_budget(raw, used_flex_query=True)["status"] == "not_configured"

    def test_accepts_a_bare_object_or_a_list(self):
        block = {
            "budgetVariability": "flexible",
            "monthlyAmounts": [
                {
                    "month": "2026-06-01",
                    "plannedCashFlowAmount": 900,
                    "actualAmount": 400,
                    "remainingAmount": 500,
                    "previousMonthRolloverAmount": 0,
                    "rolloverType": "monthly",
                }
            ],
        }

        as_object = format_flex_budget(
            {"budgetData": {"monthlyAmountsForFlexExpense": block}},
            used_flex_query=True,
        )
        as_list = format_flex_budget(
            {"budgetData": {"monthlyAmountsForFlexExpense": [block]}},
            used_flex_query=True,
        )

        assert as_object == as_list
        assert as_object["status"] == "ok"
        assert as_object["monthly"][0]["planned"] == 900

    def test_tolerates_partially_populated_amounts(self):
        raw = {
            "budgetData": {
                "monthlyAmountsForFlexExpense": {
                    "monthlyAmounts": [{"month": "2026-06-01"}]
                }
            }
        }

        result = format_flex_budget(raw, used_flex_query=True)

        assert result["status"] == "ok"
        assert result["budget_variability"] is None
        assert result["monthly"] == [
            {
                "month": "2026-06-01",
                "planned": None,
                "actual": None,
                "remaining": None,
                "rollover": None,
                "rollover_type": None,
            }
        ]

    def test_empty_amount_entries_are_ignored(self):
        raw = {
            "budgetData": {
                "monthlyAmountsForFlexExpense": {"monthlyAmounts": [{}, None]}
            }
        }

        # Nothing usable came back, so report that rather than inventing a row.
        assert format_flex_budget(raw, used_flex_query=True)["status"] == "not_configured"


class TestFormatBudgetTotals:
    def test_none_when_fallback_query_was_used(self):
        assert format_budget_totals({"budgetData": {}}, used_flex_query=False) is None

    def test_none_when_absent(self):
        assert format_budget_totals({"budgetData": {}}, used_flex_query=True) is None

    def test_maps_each_bucket(self):
        raw = {
            "budgetData": {
                "totalsByMonth": [
                    {
                        "month": "2026-06-01",
                        "totalFlexibleExpenses": {
                            "plannedAmount": 1,
                            "actualAmount": 2,
                            "remainingAmount": 3,
                            "previousMonthRolloverAmount": 4,
                        },
                        "totalFixedExpenses": None,
                    }
                ]
            }
        }

        assert format_budget_totals(raw, used_flex_query=True) == [
            {
                "month": "2026-06-01",
                "income": None,
                "expenses": None,
                "flexible": {
                    "planned": 1,
                    "actual": 2,
                    "remaining": 3,
                    "rollover": 4,
                },
                "fixed": None,
                "non_monthly": None,
            }
        ]
