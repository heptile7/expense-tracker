"""Statistics behind the dashboard and the money plan.

Conventions (all amounts in paise internally, rupees in output):
  * spending  = expense-kind debits minus refunds booked to the same category
  * income    = income-kind credits (plus uncategorized credits)
  * needs / wants come from each main category's group; sub-categories inherit it
  * invested  = net money into the Investments category (counted separately, not as spending)
  * saved     = income - needs - wants, so it includes what you invested
Transfers between your own accounts and card bill payments are left out entirely.
Purchases on a credit card count once, from the card statement; paying the bill is a transfer.
Bill payments with no imported card statement behind them are reported as "not itemised",
because what was bought with the card is then missing from spending.
"""

import calendar
import sqlite3
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

from tracker import db

PERIOD_KINDS = ("month", "3m", "fy", "12m")
PEOPLE_CATEGORY = "Payments to People"
COMMITMENT_CATEGORIES = {
    "Rent", "EMI & Loans", "Insurance", "Investments", "Bills & Utilities", "Education", "Taxes",
    "OTT & Music", "Apps & Games", "Fitness", "Domestic Help", "Mobile & Internet", "DTH & Cable",
    PEOPLE_CATEGORY,  # e.g. rent or household help paid to a person
}
NEVER_COMMITMENTS = {"Cash Withdrawal", "Fuel", "Uncategorized"}


def rupees(paise: float | None) -> float | None:
    return None if paise is None else round(paise / 100, 2)


# ── Periods ───────────────────────────────────────────────────────────────────


@dataclass
class Period:
    kind: str
    start: date
    end: date
    label: str
    months: int

    @property
    def elapsed_end(self) -> date:
        return min(self.end, date.today())

    @property
    def elapsed_days(self) -> int:
        return max(1, (self.elapsed_end - self.start).days + 1)

    @property
    def total_days(self) -> int:
        return (self.end - self.start).days + 1

    @property
    def fraction_elapsed(self) -> float:
        return min(1.0, self.elapsed_days / self.total_days)

    @property
    def in_progress(self) -> bool:
        return self.start <= date.today() < self.end


def _shift(ym: str, k: int) -> str:
    y, m = map(int, ym.split("-"))
    i = y * 12 + (m - 1) + k
    return f"{i // 12:04d}-{i % 12 + 1:02d}"


def _bounds(ym: str) -> tuple[date, date]:
    y, m = map(int, ym.split("-"))
    return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])


def _month_name(ym: str, short: bool = False) -> str:
    return _bounds(ym)[0].strftime("%b %Y" if short else "%B %Y")


def latest_month(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT MAX(substr(txn_date, 1, 7)) FROM transactions WHERE txn_date <= ?", (date.today().isoformat(),)).fetchone()
    return row[0] or date.today().strftime("%Y-%m")


def resolve_period(kind: str = "month", month: str | None = None, conn: sqlite3.Connection | None = None) -> Period:
    if kind not in PERIOD_KINDS:
        raise ValueError(f"period must be one of {', '.join(PERIOD_KINDS)}")
    if not month:
        with db.session() as c:
            cx = conn or c
            month = latest_month(cx)
            # A month still in progress (salary maybe not in yet) makes a poor default: show the last full one.
            prev = _shift(month, -1)
            if month == date.today().strftime("%Y-%m") and cx.execute(
                "SELECT 1 FROM transactions WHERE substr(txn_date, 1, 7) = ? LIMIT 1", (prev,)
            ).fetchone():
                month = prev
    if kind == "month":
        start, end = _bounds(month)
        return Period(kind, start, end, _month_name(month), 1)
    if kind == "3m":
        first = _shift(month, -2)
        return Period(kind, _bounds(first)[0], _bounds(month)[1], f"{_month_name(first, True)} – {_month_name(month, True)}", 3)
    if kind == "12m":
        first = _shift(month, -11)
        return Period(kind, _bounds(first)[0], _bounds(month)[1], f"{_month_name(first, True)} – {_month_name(month, True)}", 12)
    y, m = map(int, month.split("-"))
    fy = y if m >= 4 else y - 1  # Indian financial year: April to March
    return Period(kind, date(fy, 4, 1), date(fy + 1, 3, 31), f"FY {fy}-{(fy + 1) % 100:02d}", 12)


# ── Loading and classifying ───────────────────────────────────────────────────


def _load(conn, start: date, end: date, account_id: int | None = None) -> list[dict]:
    sql = """SELECT id, account_id, txn_date, amount_paise, direction, category_id, merchant_key, counterparty, channel,
                    vpa, linked_txn_id, category_source, rule_id
             FROM transactions WHERE txn_date BETWEEN ? AND ?"""
    params: list = [start.isoformat(), end.isoformat()]
    if account_id:
        sql += " AND account_id = ?"
        params.append(account_id)
    return [dict(r) for r in conn.execute(sql, params)]


def _classify(t: dict, tree: dict) -> tuple[str, dict]:
    """-> (bucket, category info). bucket: needs | wants | savings | income | transfer."""
    cat = tree.get(t["category_id"]) or {"top_id": None, "top_name": "Uncategorized", "kind": "expense", "grp": "wants", "name": "Uncategorized"}
    top = tree.get(cat["top_id"], cat)
    if top.get("name") == "Uncategorized" and t["direction"] == "credit":
        return "income", cat  # money in that isn't categorized yet is income-like, never negative spending
    grp = cat.get("grp") or "wants"
    if top["kind"] == "income":
        return "income", cat
    if grp == "savings":
        return "savings", cat
    if top["kind"] == "transfer" or grp == "transfer":
        return "transfer", cat
    return ("needs" if grp == "needs" else "wants"), cat


def _signed(t: dict, bucket: str) -> int:
    """Positive = money spent / invested / earned in that bucket's natural direction."""
    a = t["amount_paise"]
    if bucket == "income":
        return a if t["direction"] == "credit" else -a
    return a if t["direction"] == "debit" else -a


def _aggregate(txns: list[dict], tree: dict) -> dict:
    out = {
        "income": 0, "needs": 0, "wants": 0, "savings": 0,
        "top": defaultdict(lambda: {"amount": 0, "count": 0, "children": defaultdict(lambda: {"amount": 0, "count": 0})}),
        "income_top": defaultdict(lambda: {"amount": 0, "count": 0}),
    }
    for t in txns:
        bucket, cat = _classify(t, tree)
        if bucket == "transfer":
            continue
        amt = _signed(t, bucket)
        out[bucket] += amt
        if bucket == "income":
            out["income_top"][cat["top_id"]]["amount"] += amt
            out["income_top"][cat["top_id"]]["count"] += 1
            continue
        top = out["top"][cat["top_id"]]
        top["amount"] += amt
        top["count"] += 1
        top["bucket"] = bucket
        child = top["children"][t["category_id"]]
        child["amount"] += amt
        child["count"] += 1
    return out


# ── Statistics ────────────────────────────────────────────────────────────────


def overview(kind: str = "month", month: str | None = None, account_id: int | None = None) -> dict:
    with db.session() as conn:
        period = resolve_period(kind, month, conn)
        tree = db.category_tree(conn)
        txns = _load(conn, period.start, period.end, account_id)
        agg = _aggregate(txns, tree)

        # "Usual": average month over the 3 months before this period, scaled to the elapsed part of it.
        base_start = _bounds(_shift(period.start.strftime("%Y-%m"), -3))[0]
        base_txns = _load(conn, base_start, period.start - timedelta(days=1), account_id)
        base_months = len({t["txn_date"][:7] for t in base_txns})
        base = _aggregate(base_txns, tree)
        scale = period.months * period.fraction_elapsed / max(base_months, 1)

        budgets = {r["category_id"]: r["monthly_paise"] for r in conn.execute("SELECT category_id, monthly_paise FROM budgets")}
        recurring = recurring_payments(conn, tree, period.elapsed_end, account_id)
        six_month_income = _monthly_average_income(conn, tree, period.elapsed_end, account_id)
        unusual = _unusual(conn, tree, txns, period, account_id)
        trend = monthly_trend(conn, tree, period.end.strftime("%Y-%m"), 12, account_id)
        months_available = [r[0] for r in conn.execute("SELECT DISTINCT substr(txn_date, 1, 7) FROM transactions ORDER BY 1 DESC")]
        review = conn.execute("SELECT COUNT(*) FROM transactions WHERE needs_review = 1").fetchone()[0]
        card_accounts = {r[0] for r in conn.execute("SELECT id FROM accounts WHERE type = 'credit_card'")}
        not_itemised = card_bills_not_itemised(conn, tree, period.start, period.elapsed_end, account_id)
        cards = [c for c in card_bills(conn) if not account_id or c["account_id"] == account_id]
    card_spent = 0
    for t in txns:
        if t["account_id"] in card_accounts:
            bucket, _ = _classify(t, tree)
            if bucket in ("needs", "wants"):
                card_spent += _signed(t, bucket)

    income, needs, wants, invested = agg["income"], agg["needs"], agg["wants"], agg["savings"]
    spent = needs + wants
    saved = income - spent

    categories = []
    for top_id, v in agg["top"].items():
        info = tree.get(top_id, {"name": "Uncategorized", "color": None, "grp": "wants"})
        if v["bucket"] == "savings" or v["amount"] <= 0:
            continue
        expected = base["top"][top_id]["amount"] * scale if base_months and top_id in base["top"] else None
        budget = budgets.get(top_id)
        budget_now = budget * period.months if budget else None
        categories.append({
            "id": top_id,
            "name": info["name"],
            "group": info.get("grp") or v["bucket"],
            "amount": rupees(v["amount"]),
            "count": v["count"],
            "share": round(v["amount"] / spent, 4) if spent > 0 else None,
            "usual": rupees(expected) if expected else None,
            "change_pct": round((v["amount"] - expected) / expected, 3) if expected and expected > 0 else None,
            "budget": rupees(budget_now),
            "budget_used": round(v["amount"] / budget_now, 3) if budget_now else None,
            "children": sorted(
                ({"id": cid, "name": tree.get(cid, {}).get("name", "Uncategorized"), "amount": rupees(c["amount"]), "count": c["count"],
                  "is_parent": cid == top_id} for cid, c in v["children"].items() if c["amount"] > 0),
                key=lambda c: -c["amount"],
            ),
        })
    categories.sort(key=lambda c: -c["amount"])

    income_rows = sorted(
        ({"name": tree.get(k, {}).get("name", "Uncategorized"), "amount": rupees(v["amount"]), "count": v["count"]}
         for k, v in agg["income_top"].items() if v["amount"] > 0),
        key=lambda r: -r["amount"],
    )

    merchants, people = _merchants(txns, tree)
    active_recurring = [r for r in recurring if r["active"]]
    commitments = sum(r["monthly"] for r in active_recurring)
    projected = rupees(spent / period.elapsed_days * period.total_days) if period.in_progress and period.kind == "month" else None

    out = {
        "period": {"kind": period.kind, "label": period.label, "start": period.start.isoformat(), "end": period.end.isoformat(),
                   "in_progress": period.in_progress, "elapsed_days": period.elapsed_days, "months": period.months},
        "months": months_available,
        "has_data": bool(months_available),
        "income": rupees(income),
        "spent": rupees(spent),
        "needs": rupees(needs),
        "wants": rupees(wants),
        "invested": rupees(invested),
        "saved": rupees(saved),
        "savings_rate": round(saved / income, 3) if income > 0 else None,
        "invest_rate": round(invested / income, 3) if income > 0 else None,
        "split": {k: (round(v / income, 3) if income > 0 else None) for k, v in (("needs", needs), ("wants", wants), ("saved", saved))},
        "daily_average": rupees(spent / period.elapsed_days),
        "projected_spend": projected,
        "categories": categories,
        "income_sources": income_rows,
        "top_merchants": merchants[:10],
        "people": people[:8],
        "people_total": round(sum(p["amount"] for p in people), 2),
        "recurring": recurring,
        "recurring_monthly": round(commitments, 2),
        "recurring_share_of_income": round(commitments / rupees(six_month_income), 3) if six_month_income > 0 else None,
        "unusual": unusual,
        "trend": trend,
        "baseline_months": base_months,
        "needs_review": review,
        "card_spent": rupees(card_spent),
        "card_bills_not_itemised": not_itemised,
        "cards": cards,
    }
    out["facts"] = facts(out)
    return out


# ── Credit cards ──────────────────────────────────────────────────────────────


def _card_payment_ids(tree: dict) -> set[int]:
    return {cid for cid, c in tree.items() if c["top_name"] == "Credit Card Payment"}


def card_bills_not_itemised(conn, tree: dict, start: date, end: date, account_id: int | None = None) -> dict:
    """Card bills paid from a bank account in [start, end] with no imported card statement behind them.

    A payment counts as itemised when it was paired with the card's own record of it, or when its
    date falls inside a card statement you imported (the purchases it pays for are then on record).
    """
    ids = _card_payment_ids(tree)
    if not ids:
        return {"amount": 0, "count": 0}
    covered = [(r[0], r[1]) for r in conn.execute(
        """SELECT period_start, date(period_end, '+5 days') FROM statements
           WHERE kind = 'credit_card' AND period_start IS NOT NULL AND period_end IS NOT NULL""")]
    sql = f"""SELECT t.txn_date, t.amount_paise FROM transactions t JOIN accounts a ON a.id = t.account_id
              WHERE a.type != 'credit_card' AND t.direction = 'debit' AND t.linked_txn_id IS NULL
                AND t.category_id IN ({','.join('?' * len(ids))}) AND t.txn_date BETWEEN ? AND ?"""
    params: list = [*ids, start.isoformat(), end.isoformat()]
    if account_id:
        sql += " AND t.account_id = ?"
        params.append(account_id)
    rows = [r for r in conn.execute(sql, params) if not any(a <= r[0] <= b for a, b in covered)]
    return {"amount": rupees(sum(r[1] for r in rows)), "count": len(rows)}


def card_bills(conn, today: date | None = None) -> list[dict]:
    """Each credit card's latest bill: amount due, due date, how much has been paid since, and the status."""
    today = today or date.today()
    pay_ids = _card_payment_ids(db.category_tree(conn))
    cards = conn.execute("SELECT id, name, bank, last4 FROM accounts WHERE type = 'credit_card' ORDER BY name").fetchall()
    out = []
    for card in cards:
        s = conn.execute(
            """SELECT id, COALESCE(statement_date, period_end) AS stmt_date, period_start, period_end, due_date,
                      total_due_paise, min_due_paise
               FROM statements WHERE account_id = ? AND kind = 'credit_card'
               ORDER BY COALESCE(statement_date, period_end) DESC, id DESC LIMIT 1""",
            (card["id"],),
        ).fetchone()
        if not s:
            out.append({"account_id": card["id"], "name": card["name"], "last4": card["last4"], "status": "no_statement"})
            continue
        paid = _paid_since(conn, card, s["stmt_date"], pay_ids, len(cards))
        spent = conn.execute(
            "SELECT COALESCE(SUM(amount_paise), 0) FROM transactions WHERE statement_id = ? AND direction = 'debit' "
            "AND category_source != 'transfer'",
            (s["id"],),
        ).fetchone()[0]
        total, minimum = s["total_due_paise"], s["min_due_paise"]
        due = date.fromisoformat(s["due_date"]) if s["due_date"] else None
        if total is None:
            status = "unknown"
        elif total <= 0 or paid >= total:
            status = "paid"
        elif due and due < today:
            status = "part_paid" if minimum and paid >= minimum else "overdue"
        else:
            status = "due"
        out.append({
            "account_id": card["id"], "name": card["name"], "last4": card["last4"],
            "statement_date": s["stmt_date"], "period_start": s["period_start"], "period_end": s["period_end"],
            "due_date": s["due_date"], "days_left": (due - today).days if due else None,
            "total_due": rupees(total), "min_due": rupees(minimum), "paid": rupees(paid),
            "left_to_pay": rupees(max(total - paid, 0)) if total is not None else None,
            "spent": rupees(spent), "status": status,
        })
    return out


def _paid_since(conn, card, since: str, pay_ids: set[int], n_cards: int) -> int:
    """Payments towards this card after its statement date: the card's own record of them, plus bank-side
    bill payments the card hasn't shown yet (matched by the card's last 4 digits, or to your only card)."""
    on_card = conn.execute(
        "SELECT COALESCE(SUM(amount_paise), 0) FROM transactions WHERE account_id = ? AND direction = 'credit' "
        "AND category_source = 'transfer' AND txn_date > ?",
        (card["id"], since),
    ).fetchone()[0]
    if not pay_ids:
        return on_card
    rows = conn.execute(
        f"""SELECT t.amount_paise, t.narration_raw FROM transactions t JOIN accounts a ON a.id = t.account_id
            WHERE a.type != 'credit_card' AND t.direction = 'debit' AND t.linked_txn_id IS NULL AND t.txn_date > ?
              AND t.category_id IN ({','.join('?' * len(pay_ids))})""",
        (since, *pay_ids),
    ).fetchall()
    last4 = card["last4"]
    from_bank = sum(r[0] for r in rows if (last4 and last4 in (r[1] or "")) or n_cards == 1)
    return on_card + from_bank


def _merchants(txns: list[dict], tree: dict) -> tuple[list[dict], list[dict]]:
    shops: dict[str, dict] = {}
    people: dict[str, dict] = {}
    for t in txns:
        bucket, cat = _classify(t, tree)
        if bucket not in ("needs", "wants") or t["direction"] != "debit":
            continue
        target = people if tree.get(cat["top_id"], {}).get("name") == PEOPLE_CATEGORY else shops
        m = target.setdefault(t["merchant_key"], {"name": t["counterparty"] or t["merchant_key"].partition(":")[2],
                                                  "category": cat.get("name"), "amount": 0, "count": 0})
        m["amount"] += t["amount_paise"]
        m["count"] += 1
    def finish(d):
        rows = sorted(d.values(), key=lambda m: -m["amount"])
        for m in rows:
            m["average"] = rupees(m["amount"] / m["count"])
            m["amount"] = rupees(m["amount"])
        return rows
    return finish(shops), finish(people)


def _commitment_key(cat: dict) -> str | None:
    """The category a commitment is tracked under: the most specific one that is commitment-type."""
    if cat.get("name") in COMMITMENT_CATEGORIES and cat.get("name") != PEOPLE_CATEGORY:
        return cat["name"]
    if cat.get("top_name") in COMMITMENT_CATEGORIES and cat.get("top_name") != PEOPLE_CATEGORY:
        return cat["top_name"]
    return None


def recurring_payments(conn, tree: dict, as_of: date, account_id: int | None = None) -> list[dict]:
    """Regular monthly outgoings over the last 6 months.

    * Commitment-type categories (rent, EMIs, SIPs, insurance, bills, subscriptions) are tracked as a
      monthly total per category, whoever they're paid to: rent split into two payments, a landlord who
      changed UPI ID, or several SIPs on the same day all count. Present in 3+ of the last 6 months.
    * Anything else must be one payee charging (almost) the same amount once or twice a month,
      e.g. a gym, a newspaper, household help paid by UPI.
    """
    start = as_of - timedelta(days=183)
    by_cat: dict[str, list[dict]] = defaultdict(list)
    by_payee: dict[str, list[dict]] = defaultdict(list)
    for t in _load(conn, start, as_of, account_id):
        bucket, cat = _classify(t, tree)
        if t["direction"] != "debit" or bucket not in ("needs", "wants", "savings") or cat.get("top_name") in NEVER_COMMITMENTS:
            continue
        t["_cat"] = cat
        key = _commitment_key(cat)
        if key:
            by_cat[key].append(t)
        else:
            by_payee[t["merchant_key"]].append(t)

    out = []
    for key, txns in by_cat.items():
        months = defaultdict(int)
        for t in txns:
            months[t["txn_date"][:7]] += t["amount_paise"]
        if len(months) < 3:
            continue
        payees = defaultdict(int)
        for t in txns:
            payees[t["counterparty"] or t["merchant_key"].partition(":")[2]] += t["amount_paise"]
        monthly = statistics.median(months.values())
        out.append(_entry(key, txns, monthly, len(months), as_of,
                          payee=max(payees, key=payees.get), payees=len(payees), per_month=None))

    for key, txns in by_payee.items():
        months = defaultdict(list)
        for t in txns:
            months[t["txn_date"][:7]].append(t["amount_paise"])
        if len(months) < 3:
            continue
        per_month = statistics.median(len(v) for v in months.values())
        if per_month > 2:
            continue  # frequent purchases (food delivery, cabs): habits, not commitments
        amounts = [a for v in months.values() for a in v]
        med = statistics.median(amounts)
        if sum(1 for a in amounts if abs(a - med) <= 0.05 * med) / len(amounts) < 0.7:
            continue
        cat = txns[-1]["_cat"]
        out.append(_entry(cat.get("name"), txns, med * per_month, len(months), as_of,
                          payee=txns[-1]["counterparty"] or key.partition(":")[2], payees=1, per_month=per_month))
    out.sort(key=lambda r: (not r["active"], -r["monthly"]))
    return out


def _entry(category: str, txns: list[dict], monthly: float, months_seen: int, as_of: date, payee: str, payees: int, per_month) -> dict:
    last = max(t["txn_date"] for t in txns)
    cat = txns[-1]["_cat"]
    return {
        "payee": payee if payees == 1 else f"{category} ({payees} payees)",
        "main_payee": payee,
        "payees": payees,
        "category": category,
        "top_category": cat.get("top_name"),
        "group": cat.get("grp"),
        "per_month": per_month,
        "monthly": rupees(monthly),
        "months_seen": months_seen,
        "last_date": last,
        "next_expected": (date.fromisoformat(last) + timedelta(days=30)).isoformat(),
        "active": date.fromisoformat(last) >= as_of - timedelta(days=45),
    }


def _monthly_average_income(conn, tree: dict, as_of: date, account_id: int | None) -> int:
    txns = _load(conn, as_of - timedelta(days=183), as_of, account_id)
    months = len({t["txn_date"][:7] for t in txns}) or 1
    return sum(_signed(t, "income") for t in txns if _classify(t, tree)[0] == "income") // months


def _unusual(conn, tree: dict, txns: list[dict], period: Period, account_id: int | None) -> list[dict]:
    """Single payments far above what's normal for their category (last 6 months)."""
    history = _load(conn, period.elapsed_end - timedelta(days=183), period.elapsed_end, account_id)
    typical: dict[int, list[int]] = defaultdict(list)
    for t in history:
        bucket, cat = _classify(t, tree)
        if bucket in ("needs", "wants") and t["direction"] == "debit":
            typical[cat["top_id"]].append(t["amount_paise"])
    medians = {k: statistics.median(v) for k, v in typical.items() if len(v) >= 3}
    everything = [a for v in typical.values() for a in v]
    overall = statistics.median(everything) if len(everything) >= 5 else None  # for categories with little history
    flagged = []
    for t in txns:
        bucket, cat = _classify(t, tree)
        if bucket not in ("needs", "wants") or t["direction"] != "debit":
            continue
        med = medians.get(cat["top_id"]) or overall
        if med and t["amount_paise"] >= max(3 * med, 200_000):
            flagged.append({"id": t["id"], "date": t["txn_date"], "payee": t["counterparty"], "category": cat.get("name"),
                            "amount": rupees(t["amount_paise"]), "times_usual": round(t["amount_paise"] / med, 1)})
    flagged.sort(key=lambda r: -r["amount"])
    return flagged[:6]


def monthly_trend(conn, tree: dict, last_month: str, n: int = 12, account_id: int | None = None) -> list[dict]:
    first = _shift(last_month, -(n - 1))
    txns = _load(conn, _bounds(first)[0], _bounds(last_month)[1], account_id)
    by_month: dict[str, list[dict]] = defaultdict(list)
    for t in txns:
        by_month[t["txn_date"][:7]].append(t)
    out = []
    for i in range(n):
        m = _shift(first, i)
        a = _aggregate(by_month.get(m, []), tree)
        spent = a["needs"] + a["wants"]
        out.append({
            "month": m, "income": rupees(a["income"]), "expense": rupees(spent), "needs": rupees(a["needs"]),
            "wants": rupees(a["wants"]), "invested": rupees(a["savings"]),
            "savings_rate": round((a["income"] - spent) / a["income"], 3) if a["income"] > 0 else None,
            "has_data": m in by_month,
        })
    return out


# ── Plain-language facts ──────────────────────────────────────────────────────


def _inr(v: float) -> str:
    """₹12,34,567 (Indian digit grouping), no decimals."""
    digits = f"{abs(v):.0f}"
    if len(digits) > 3:
        last3, rest = digits[-3:], digits[:-3]
        groups = []
        while len(rest) > 2:
            groups.insert(0, rest[-2:])
            rest = rest[:-2]
        if rest:
            groups.insert(0, rest)
        digits = ",".join(groups + [last3])
    return f"{'−' if v < 0 else ''}₹{digits}"


def facts(o: dict) -> list[dict]:
    """Short observations for the dashboard and the plan. tone: good | warn | info."""
    out: list[dict] = []
    p = o["period"]
    so_far = " so far" if p["in_progress"] else ""
    rate = o["savings_rate"]
    if p["in_progress"] and o["saved"] < 0:
        out.append({"tone": "info", "text": f"Income so far ({_inr(o['income'] or 0)}) is below spending so far ({_inr(o['spent'])}). "
                                            "If your salary hasn't come in yet this month, the picture will change when it does."})
    elif o["income"] and rate is not None and rate < 0:
        out.append({"tone": "warn", "text": f"You spent {_inr(-o['saved'])} more than came in during {p['label']}."})
    elif o["income"]:
        tone = "good" if rate >= 0.2 else ("warn" if rate < 0.1 else "info")
        out.append({"tone": tone, "text": f"You kept {rate:.0%} of your income{so_far} in {p['label']} "
                                         f"({_inr(o['saved'])} of {_inr(o['income'])}). A common target is 20% or more."})
    if o["invested"] and o["income"] and o["invest_rate"] <= 1:
        out.append({"tone": "good", "text": f"{_inr(o['invested'])} went into investments ({o['invest_rate']:.0%} of income)."})
    elif o["invested"]:
        out.append({"tone": "good", "text": f"{_inr(o['invested'])} went into investments."})
    ups = [c for c in o["categories"] if c["change_pct"] is not None and c["change_pct"] >= 0.2 and c["amount"] - (c["usual"] or 0) >= 500]
    for c in sorted(ups, key=lambda c: -(c["amount"] - c["usual"]))[:3]:
        out.append({"tone": "warn", "text": f"{c['name']}: {_inr(c['amount'])}{so_far}, {c['change_pct']:.0%} above your usual "
                                            f"{_inr(c['usual'])} for this point."})
    downs = [c for c in o["categories"] if c["change_pct"] is not None and c["change_pct"] <= -0.2 and (c["usual"] or 0) - c["amount"] >= 500]
    for c in sorted(downs, key=lambda c: c["amount"] - c["usual"])[:2]:
        out.append({"tone": "good", "text": f"{c['name']}: {_inr(c['amount'])}, {abs(c['change_pct']):.0%} below your usual {_inr(c['usual'])}."})
    for c in o["categories"]:
        if c["budget_used"] is not None and c["budget_used"] > 1:
            out.append({"tone": "warn", "text": f"{c['name']} is over budget: {_inr(c['amount'])} of {_inr(c['budget'])}."})
    if o["recurring_monthly"]:
        share = o["recurring_share_of_income"]
        out.append({"tone": "info", "text": f"Regular commitments (rent, EMIs, SIPs, subscriptions) come to about {_inr(o['recurring_monthly'])} a month"
                                            + (f", {share:.0%} of your usual income." if share else ".")})
    for c in o["categories"]:
        for ch in c["children"]:
            if ch["name"] == "Food Delivery" and ch["count"] >= 6:
                out.append({"tone": "info", "text": f"{ch['count']} food delivery orders{so_far}, {_inr(ch['amount'])} in total "
                                                    f"(about {_inr(ch['amount'] / ch['count'])} each)."})
    cash = next((c for c in o["categories"] if c["name"] == "Cash Withdrawal"), None)
    if cash and o["spent"] and cash["amount"] / o["spent"] >= 0.1:
        out.append({"tone": "info", "text": f"{_inr(cash['amount'])} was withdrawn as cash ({cash['share']:.0%} of spending), so where it went isn't tracked."})
    if o["people_total"] and o["spent"] and o["people_total"] / o["spent"] >= 0.1:
        out.append({"tone": "info", "text": f"{_inr(o['people_total'])} went to individuals by UPI/IMPS. Splitting these into what they were for "
                                            "(rent, household help, family) sharpens the picture."})
    gap = o.get("card_bills_not_itemised") or {}
    if gap.get("amount", 0) >= 500:
        out.append({"tone": "warn", "text": f"{_inr(gap['amount'])} went to credit card bills{so_far} with no card statement "
                                            "imported for them, so what you bought with the card isn't in these numbers. "
                                            "Import your card statements to see it."})
    if o.get("card_spent") and o["spent"] and o["card_spent"] > 0:
        out.append({"tone": "info", "text": f"{_inr(o['card_spent'])} of your spending{so_far} was on credit cards "
                                            f"({o['card_spent'] / o['spent']:.0%})."})
    for c in o.get("cards") or []:
        if c.get("status") in ("overdue", "part_paid"):
            out.append({"tone": "warn", "text": f"{c['name']}: the bill of {_inr(c['total_due'])} was due on {c['due_date']} "
                                                f"and {_inr(c['left_to_pay'])} is still unpaid as far as your statements show."})
    unc = next((c for c in o["categories"] if c["name"] == "Uncategorized"), None)
    if unc and unc["amount"] >= 1000:
        out.append({"tone": "info", "text": f"{_inr(unc['amount'])} is still uncategorized: see the Review tab."})
    return out
