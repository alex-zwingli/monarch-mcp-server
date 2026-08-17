"""Budget tools."""

import calendar
import logging
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from gql import gql
from monarchmoney import MonarchMoney

try:  # gql raises this when the server answers but refuses the query itself.
    from gql.transport.exceptions import TransportQueryError
except ImportError:  # pragma: no cover - defensive, gql always ships it today
    TransportQueryError = ()

from monarch_mcp_server.app import mcp
from monarch_mcp_server.client import get_monarch_client
from monarch_mcp_server.helpers import json_success, json_error

logger = logging.getLogger(__name__)

# The upstream SDK's get_budgets() requests category-group fields (e.g.
# budgetVariability/rolloverPeriod) that Monarch's current API rejects for some
# accounts, so it can fail outright. This narrower query asks only for fields
# the current API still returns.
#
# Both documents below render from this one template, so the categoryGroups
# selection is *structurally* identical between them -- adding flex support
# cannot widen it by accident.
_BUDGET_DOCUMENT = """
    query %(operation)s($startDate: Date!, $endDate: Date!) {
      budgetData(startMonth: $startDate, endMonth: $endDate) {
        monthlyAmountsByCategory {
          category {
            id
            __typename
          }
          monthlyAmounts {
            month
            plannedCashFlowAmount
            plannedSetAsideAmount
            actualAmount
            remainingAmount
            __typename
          }
          __typename
        }%(flex_selections)s
        __typename
      }
      categoryGroups {
        id
        name
        type
        categories {
          id
          name
          __typename
        }
        __typename
      }
    }
"""

# Bucket-level amounts Monarch exposes for the "fixed_and_flex" budget system.
# Under that system the Flexible section carries a single amount covering every
# category beneath it; without these selections that number is invisible and
# spending-vs-budget for Flexible cannot be computed (issue #103).
#
# Note: `budgetVariability` appears here under monthlyAmountsForFlexExpense,
# a *different* subtree from the categoryGroups.budgetVariability field
# implicated in the original failure.
_FLEX_SELECTIONS = """
        monthlyAmountsForFlexExpense {
          budgetVariability
          monthlyAmounts {
            month
            plannedCashFlowAmount
            actualAmount
            remainingAmount
            previousMonthRolloverAmount
            rolloverType
            __typename
          }
          __typename
        }
        totalsByMonth {
          month
          totalFlexibleExpenses {
            plannedAmount
            actualAmount
            remainingAmount
            previousMonthRolloverAmount
            __typename
          }
          totalFixedExpenses {
            plannedAmount
            actualAmount
            remainingAmount
            previousMonthRolloverAmount
            __typename
          }
          totalNonMonthlyExpenses {
            plannedAmount
            actualAmount
            remainingAmount
            previousMonthRolloverAmount
            __typename
          }
          __typename
        }"""

BUDGET_QUERY = gql(
    _BUDGET_DOCUMENT % {"operation": "MCPBudgetData", "flex_selections": ""}
)

# Tried first; falls back to BUDGET_QUERY when Monarch refuses these fields.
BUDGET_QUERY_FLEX = gql(
    _BUDGET_DOCUMENT
    % {"operation": "MCPBudgetDataFlex", "flex_selections": _FLEX_SELECTIONS}
)

# Cached for the life of the process: None = not yet probed, True = the flex
# fields work, False = this account rejects them so don't pay the failed
# round-trip again.
_flex_supported: Optional[bool] = None

# Supplementary text match. Monarch does not return standard GraphQL validation
# wording -- a rejected field comes back as a generic "Something went wrong
# while processing" -- so exception *type* is the primary signal and these are
# only a backstop for transports that do surface the usual messages.
_SCHEMA_REJECTION_MARKERS = (
    "cannot query field",
    "unknown field",
    "no field named",
    "unknown argument",
    "did you mean",
    "validation error",
)


def reset_flex_support() -> None:
    """Clear the cached flex-support result. Intended for tests."""
    global _flex_supported
    _flex_supported = None


def _is_query_rejection(exc: Exception) -> bool:
    """True when the server answered but refused the query itself.

    ``TransportQueryError`` is what gql raises when a response carries GraphQL
    ``errors``; HTTP failures (401, 5xx) raise ``TransportServerError`` and
    connection problems raise their own types, so those fall through to the
    caller rather than being mistaken for "this account has no flex bucket".
    """
    if TransportQueryError and isinstance(exc, TransportQueryError):
        return True
    text = str(exc).lower()
    return any(marker in text for marker in _SCHEMA_REJECTION_MARKERS)


def current_month_range() -> tuple[str, str]:
    """Return the current month bounds as ISO date strings."""
    today = date.today()
    last_day = calendar.monthrange(today.year, today.month)[1]
    return today.replace(day=1).isoformat(), today.replace(day=last_day).isoformat()


async def get_budget_data(
    client: MonarchMoney,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> Tuple[Dict[str, Any], bool]:
    """Fetch budget data, preferring the query that includes flex bucket totals.

    Returns ``(raw_response, used_flex_query)``. When Monarch rejects the flex
    selections this falls back to :data:`BUDGET_QUERY`, so accounts that do not
    support them behave exactly as they did before.
    """
    global _flex_supported

    default_start, default_end = current_month_range()
    variables = {
        "startDate": start_date or default_start,
        "endDate": end_date or default_end,
    }

    rejection: Optional[Exception] = None

    if _flex_supported is not False:
        try:
            data = await client.gql_call(
                operation="MCPBudgetDataFlex",
                graphql_query=BUDGET_QUERY_FLEX,
                variables=variables,
            )
            _flex_supported = True
            return data, True
        except Exception as exc:
            if not _is_query_rejection(exc):
                raise
            rejection = exc

    # Retry narrow. Note the latch is only set *after* this succeeds: if the
    # fallback fails too, the problem was never flex-specific (an expired
    # session refuses both queries), so it propagates and flex stays un-probed
    # rather than being disabled for the rest of the process.
    data = await client.gql_call(
        operation="MCPBudgetData",
        graphql_query=BUDGET_QUERY,
        variables=variables,
    )

    if rejection is not None:
        logger.warning(
            "Monarch rejected the flex budget fields; using the narrow budget "
            "query for the rest of this process: %s",
            rejection,
        )
        _flex_supported = False

    return data, False


def format_budget_data(budget_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Format Monarch budget data into one row per category/month."""
    category_lookup: Dict[str, Dict[str, Optional[str]]] = {}
    for group in budget_data.get("categoryGroups", []):
        for category in group.get("categories", []):
            category_id = category.get("id")
            if category_id:
                category_lookup[category_id] = {
                    "name": category.get("name"),
                    "category_group": group.get("name"),
                }

    budget_rows = []
    monthly_by_category = (
        budget_data.get("budgetData", {}).get("monthlyAmountsByCategory", [])
    )
    for category_budget in monthly_by_category:
        category_id = (category_budget.get("category") or {}).get("id")
        category_info = category_lookup.get(category_id, {})
        for monthly_amount in category_budget.get("monthlyAmounts", []):
            budget_rows.append(
                {
                    "id": category_id,
                    "name": category_info.get("name"),
                    "planned": monthly_amount.get("plannedCashFlowAmount"),
                    "actual": monthly_amount.get("actualAmount"),
                    "remaining": monthly_amount.get("remainingAmount"),
                    "category_group": category_info.get("category_group"),
                    "month": monthly_amount.get("month"),
                }
            )

    return budget_rows


def _totals_entry(node: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Normalize one of Monarch's planned/actual/remaining total objects."""
    if not node:
        return None
    return {
        "planned": node.get("plannedAmount"),
        "actual": node.get("actualAmount"),
        "remaining": node.get("remainingAmount"),
        "rollover": node.get("previousMonthRolloverAmount"),
    }


def format_flex_budget(
    budget_data: Dict[str, Any], used_flex_query: bool
) -> Dict[str, Any]:
    """Summarize the Flexible bucket, always reporting *why* it may be empty.

    ``status`` separates three cases that would otherwise be indistinguishable
    -- and reading "no flex budget" as "$0" would be wrong:

    - ``unsupported``    Monarch rejected the flex fields; the fallback ran
    - ``not_configured`` the query worked but this account has no flex bucket
    - ``ok``             a flex bucket is present
    """
    if not used_flex_query:
        return {"status": "unsupported", "budget_variability": None, "monthly": []}

    flex = (budget_data.get("budgetData") or {}).get("monthlyAmountsForFlexExpense")
    if isinstance(flex, list):
        entries = flex
    elif flex:
        entries = [flex]
    else:
        entries = []

    variability: Optional[str] = None
    monthly: List[Dict[str, Any]] = []
    for entry in entries:
        if not entry:
            continue
        variability = entry.get("budgetVariability") or variability
        for amount in entry.get("monthlyAmounts") or []:
            if not amount:
                continue
            monthly.append(
                {
                    "month": amount.get("month"),
                    "planned": amount.get("plannedCashFlowAmount"),
                    "actual": amount.get("actualAmount"),
                    "remaining": amount.get("remainingAmount"),
                    "rollover": amount.get("previousMonthRolloverAmount"),
                    "rollover_type": amount.get("rolloverType"),
                }
            )

    if not monthly:
        return {
            "status": "not_configured",
            "budget_variability": variability,
            "monthly": [],
        }

    return {"status": "ok", "budget_variability": variability, "monthly": monthly}


def format_budget_totals(
    budget_data: Dict[str, Any], used_flex_query: bool
) -> Optional[List[Dict[str, Any]]]:
    """Per-month fixed/flexible/non-monthly totals, or None when unavailable."""
    if not used_flex_query:
        return None

    totals = (budget_data.get("budgetData") or {}).get("totalsByMonth")
    if not totals:
        return None

    return [
        {
            "month": total.get("month"),
            "flexible": _totals_entry(total.get("totalFlexibleExpenses")),
            "fixed": _totals_entry(total.get("totalFixedExpenses")),
            "non_monthly": _totals_entry(total.get("totalNonMonthlyExpenses")),
        }
        for total in totals
        if total
    ]


@mcp.tool()
async def get_budgets(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> str:
    """
    Get budget information from Monarch Money.

    Args:
        start_date: Start month in YYYY-MM-DD format (defaults to the current month)
        end_date: End month in YYYY-MM-DD format (defaults to the current month)

    Returns:
        A JSON object with:

        ``data`` - one row per budgeted category per month, each with ``id``
        (category id), ``name``, ``planned`` (planned cash-flow amount),
        ``actual``, ``remaining``, ``category_group`` and ``month``.

        ``flex`` - the all-up Flexible bucket for accounts on Monarch's
        "fixed_and_flex" budget system, with ``status`` (``ok``,
        ``not_configured`` or ``unsupported``), ``budget_variability`` and a
        ``monthly`` list. Under flex budgeting a single amount covers every
        category in the Flexible section, so this -- not the per-category rows
        -- is the number to compare spending against. A ``status`` other than
        ``ok`` means no amount is available; do NOT treat that as zero.

        ``totals`` - per-month ``flexible`` / ``fixed`` / ``non_monthly``
        totals, or null when this account does not expose them.
    """
    try:
        client = await get_monarch_client()
        raw, used_flex_query = await get_budget_data(client, start_date, end_date)
        return json_success(
            {
                "tool": "get_budgets",
                "args": {"start_date": start_date, "end_date": end_date},
                "data": format_budget_data(raw),
                "flex": format_flex_budget(raw, used_flex_query),
                "totals": format_budget_totals(raw, used_flex_query),
            }
        )
    except Exception as e:
        return json_error("get_budgets", e)


@mcp.tool()
async def set_budget_amount(
    amount: float,
    category_id: Optional[str] = None,
    category_group_id: Optional[str] = None,
    start_date: Optional[str] = None,
    apply_to_future: bool = False,
) -> str:
    """
    Set or update a budget amount for a category or category group.

    Use get_budgets() first to see current budgets and category IDs.
    Use get_categories() or get_category_groups() to find category/group IDs.

    Note: this cannot set the all-up Flexible bucket amount -- that bucket is
    not a category group. Use set_flexible_budget() for it.

    Args:
        amount: The budget amount to set. Use 0 to clear/unset the budget.
        category_id: The ID of the category to budget (cannot use with category_group_id)
        category_group_id: The ID of the category group to budget (cannot use with category_id)
        start_date: The month to set budget for in YYYY-MM-DD format (defaults to current month)
        apply_to_future: Whether to apply this amount to all future months (default: False)

    Returns:
        Result of the budget update.

    Examples:
        Set grocery budget to $600 for current month:
            set_budget_amount(amount=600, category_id="cat_groceries_123")

        Set dining budget to $200 and apply to all future months:
            set_budget_amount(amount=200, category_id="cat_dining_456", apply_to_future=True)

        Clear a budget (set to 0):
            set_budget_amount(amount=0, category_id="cat_123")
    """
    try:
        if category_id and category_group_id:
            return json_success({
                "success": False,
                "error": "Cannot specify both category_id and category_group_id. Choose one."
            })

        if not category_id and not category_group_id:
            return json_success({
                "success": False,
                "error": "Must specify either category_id or category_group_id."
            })

        client = await get_monarch_client()

        params: Dict[str, Any] = {
            "amount": amount,
            "apply_to_future": apply_to_future,
        }

        if category_id:
            params["category_id"] = category_id
        if category_group_id:
            params["category_group_id"] = category_group_id
        if start_date:
            params["start_date"] = start_date

        result = await client.set_budget_amount(**params)

        return json_success({
            "success": True,
            "message": f"Budget set to ${amount:.2f}" + (" for all future months" if apply_to_future else ""),
            "result": result
        })
    except Exception as e:
        return json_error("set_budget_amount", e)


@mcp.tool()
async def set_flexible_budget(
    amount: float,
    start_date: Optional[str] = None,
    apply_to_future: bool = False,
) -> str:
    """
    Set the all-up Flexible bucket budget (Monarch's "fixed_and_flex" system).

    This is the single amount covering every category in the Flexible section.
    It is not a category or a category group, so set_budget_amount() cannot
    reach it.

    Args:
        amount: The budget amount to set. Use 0 to clear/unset it.
        start_date: The month to set in YYYY-MM-DD format (defaults to current month)
        apply_to_future: Whether to apply this amount to all future months

    Returns:
        Result of the update.
    """
    try:
        if _flex_supported is False:
            return json_success({
                "success": False,
                "error": (
                    "This account does not appear to support Monarch's flexible "
                    "budget bucket -- the API rejected the flex fields on an "
                    "earlier get_budgets call."
                ),
            })

        client = await get_monarch_client()

        updater = getattr(client, "update_flexible_budget", None)
        if updater is None:
            return json_success({
                "success": False,
                "error": (
                    "The installed monarchmoney client has no "
                    "update_flexible_budget(); upgrade monarchmoneycommunity."
                ),
            })

        result = await updater(
            amount=amount,
            start_date=start_date,
            apply_to_future=apply_to_future,
        )

        return json_success({
            "success": True,
            "message": f"Flexible budget set to ${amount:.2f}"
                       + (" for all future months" if apply_to_future else ""),
            "result": result,
        })
    except Exception as e:
        return json_error("set_flexible_budget", e)
