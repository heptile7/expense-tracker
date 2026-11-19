"""
Expense Tracker — MCP Server
Lets Claude Desktop (or any MCP client) answer questions about your spending and fix
categories. Corrections made here teach the categorizer exactly like the web UI does.

Usage (Claude Desktop): add to claude_desktop_config.json
  {
    "mcpServers": {
      "expense-tracker": {
        "command": "E:/Fun/expense-tracker/.venv/Scripts/python.exe",
        "args": ["E:/Fun/expense-tracker/mcp_server.py"]
      }
    }
  }
"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

import logging
from datetime import date

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from tracker import db, service

READ_ONLY = ToolAnnotations(read_only_hint=True)

logging.basicConfig(level=logging.WARNING, stream=sys.stderr)  # stdout is the MCP channel
db.init_db()

mcp = MCPServer(
    "expense-tracker",
    instructions=(
        "Personal expense tracker built from the user's Indian bank statements (HDFC, City Union Bank). "
        "Amounts are in Indian rupees. Spending totals exclude transfers between the user's own accounts, "
        "credit card bill payments and investments. Dates are ISO yyyy-mm-dd. "
        "Use list_categories for valid category names before recategorizing. "
        "recategorize with apply_to_merchant=True teaches the tracker: every past and future payment to "
        "that payee gets the new category."
    ),
)


def _txn_brief(t: dict) -> dict:
    return {
        "id": t["id"],
        "date": t["txn_date"],
        "payee": t["counterparty"],
        "amount_inr": t["amount"],
        "direction": t["direction"],
        "category": t["category"],
        "categorized_by": t["category_source"],
        "confidence": t["confidence"],
        "account": t["account_name"],
        "narration": t["narration_raw"],
        "note": t["note"],
    }


@mcp.tool(annotations=READ_ONLY)
def spending_summary(
    start_date: str | None = None,
    end_date: str | None = None,
    group_by: str = "category",
    account: str | None = None,
) -> dict:
    """Total spending and income for a date range.

    Args:
        start_date: inclusive, yyyy-mm-dd. Defaults to the first day of the current month.
        end_date: inclusive, yyyy-mm-dd. Defaults to today.
        group_by: "category", "merchant", "month" or "account".
        account: optional account name to restrict to.
    """
    today = date.today()
    start_date = start_date or today.replace(day=1).isoformat()
    end_date = end_date or today.isoformat()
    account_id = _account_id(account) if account else None
    rows = service.spending_summary(start_date, end_date, group_by, account_id)
    expense = round(sum(r["amount"] for r in rows if r["kind"] == "expense"), 2)
    income = round(sum(r["amount"] for r in rows if r["kind"] == "income"), 2)
    return {
        "start_date": start_date,
        "end_date": end_date,
        "total_spent_inr": expense,
        "total_income_inr": income,
        "rows": [{k: r[k] for k in ("label", "kind", "amount", "count")} for r in rows],
    }


@mcp.tool(annotations=READ_ONLY)
def search_transactions(
    query: str | None = None,
    category: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    min_amount: float | None = None,
    max_amount: float | None = None,
    direction: str | None = None,
    needs_review: bool = False,
    limit: int = 50,
) -> dict:
    """Find transactions. query matches payee, narration or note text.

    Args:
        direction: "debit" (money out) or "credit" (money in).
        needs_review: only low-confidence or uncategorized transactions.
        limit: max rows returned (up to 200).
    """
    res = service.list_transactions(
        limit=min(limit, 200), q=query, category=category, start_date=start_date, end_date=end_date,
        min_amount=min_amount, max_amount=max_amount, direction=direction, needs_review=needs_review,
    )
    return {"total_matches": res["total"], "transactions": [_txn_brief(t) for t in res["items"]]}


@mcp.tool(annotations=READ_ONLY)
def list_categories() -> list[dict]:
    """All categories with their kind (expense / income / transfer) and how many transactions use them."""
    return [
        {"name": c["name"], "kind": c["kind"], "description": c["description"], "transactions": c["txn_count"]}
        for c in service.list_categories()
    ]


@mcp.tool(annotations=READ_ONLY)
def review_queue(limit: int = 25) -> dict:
    """Transactions Claude was unsure about or couldn't categorize, lowest confidence first."""
    res = service.list_transactions(limit=min(limit, 200), needs_review=True, sort="confidence")
    return {"total": res["total"], "transactions": [_txn_brief(t) for t in res["items"]]}


@mcp.tool()
def recategorize(transaction_id: int, category: str, apply_to_merchant: bool = True) -> dict:
    """Change a transaction's category.

    Args:
        transaction_id: id from search_transactions or review_queue.
        category: an existing category name (see list_categories).
        apply_to_merchant: True (default) remembers this for the payee: all past and future
            transactions with the same payee get this category. False changes only this one.
    """
    return service.set_category(transaction_id, category, "merchant" if apply_to_merchant else "single")


@mcp.tool()
def add_rule(pattern: str, category: str, match_type: str = "contains", direction: str = "any") -> dict:
    """Create a rule: transactions whose narration contains `pattern` (or matches it as a regex) get `category`.

    Args:
        match_type: "contains" (case-insensitive substring) or "regex".
        direction: "any", "debit" or "credit".
    """
    out = service.create_rule(pattern, category, match_type, direction)
    return {"rule_id": out["rule"]["id"], "matched_transactions": out["matched_transactions"]}


@mcp.tool(annotations=READ_ONLY)
def list_accounts() -> list[dict]:
    """Bank accounts in the tracker, with transaction counts and the latest transaction date."""
    return [
        {"name": a["name"], "bank": a["bank"], "type": a["type"], "last4": a["last4"],
         "transactions": a["txn_count"], "latest_transaction": a["last_txn_date"]}
        for a in service.list_accounts()
    ]


def _account_id(name: str) -> int:
    for a in service.list_accounts():
        if a["name"].lower() == name.lower():
            return a["id"]
    raise ValueError(f"No account named {name!r}. Accounts: {', '.join(a['name'] for a in service.list_accounts())}")


if __name__ == "__main__":
    mcp.run()
