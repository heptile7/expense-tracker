"""Where the money went: every rupee in and out over a period, accounted for.

Money out = spending by category (purchases on cards included) + money put into investments
+ card bills with no card statement behind them + money sent to your own accounts the app
doesn't track. Money in = income by source + money taken out of investments + money from your
untracked accounts. Transfers between tracked accounts, and card bills whose purchases are on
record, are internal and left out, so nothing is counted twice. For bank accounts with a
running balance, money in minus money out matches how much the balance moved.

On top of that:
  * one-off payments (large, and not to a payee you pay regularly) are set apart from regular spending;
  * "not explained yet" collects what the categories can't tell you: card bills without a statement,
    payments to individuals, Uncategorized and Other, and likely mix-ups (a keyword in the note of a
    payment to a person, an unusually large "bank charge").
"""

import calendar
import statistics
from collections import defaultdict
from datetime import date, timedelta

from tracker import db
from tracker.insights import PEOPLE_CATEGORY, _classify, _load, resolve_period, rupees
from tracker.local_classifier import looks_like_person

ONE_OFF_MIN = 10_000_00  # paise: smaller payments are never called one-offs
BIG_PEOPLE_PAYMENT = 5_000_00
MIXUP_MIN = 1_000_00
BIG_BANK_CHARGE = 2_000_00
SELF_TRANSFER, CARD_PAYMENT, OTHER, UNCATEGORIZED = "Self Transfer", "Credit Card Payment", "Other", "Uncategorized"
# Categories a keyword in a payment note can wrongly put a payment to a person into.
_PERSON_OK = {PEOPLE_CATEGORY, "Rent", SELF_TRANSFER, "Household & Services", "Gifts & Donations", UNCATEGORIZED}


def where_it_went(kind: str = "month", month: str | None = None, account_id: int | None = None, today: date | None = None) -> dict:
    today = today or date.today()
    with db.session() as conn:
        period = resolve_period(kind, month, conn)
        tree = db.category_tree(conn)
        txns = _load(conn, period.start, period.end, account_id)
        history = _load(conn, period.end - timedelta(days=365), period.end, account_id)
        account_types = {r["id"]: r["type"] for r in conn.execute("SELECT id, type FROM accounts")}
        covered = [(r[0], r[1]) for r in conn.execute(
            """SELECT period_start, date(period_end, '+5 days') FROM statements
               WHERE kind = 'credit_card' AND period_start IS NOT NULL AND period_end IS NOT NULL""")]
        balance = _balance_change(conn, period.start, period.end, account_id)
        seed_words = {r[0]: r[1] for r in conn.execute("SELECT id, pattern FROM merchant_rules WHERE source = 'seed'")}
        own_names = [set(r[0].upper().split()) for r in conn.execute("SELECT holder_name FROM accounts WHERE holder_name IS NOT NULL")]

    out_cats: dict[int, dict] = {}
    extra_out = defaultdict(lambda: {"amount": 0, "count": 0})  # investments, card bills, own accounts, to cards
    money_in = defaultdict(lambda: {"amount": 0, "count": 0})
    spending: list[tuple[dict, dict]] = []
    investing: list[tuple[dict, dict]] = []

    for t in txns:
        bucket, cat = _classify(t, tree)
        a, debit = t["amount_paise"], t["direction"] == "debit"
        if bucket in ("needs", "wants"):
            row = out_cats.setdefault(cat["top_id"], {
                "category_id": cat["top_id"], "name": tree.get(cat["top_id"], cat).get("name") or UNCATEGORIZED,
                "group": bucket, "amount": 0, "count": 0, "children": defaultdict(int),
            })
            row["amount"] += a if debit else -a
            row["count"] += debit
            row["children"][cat.get("name") or UNCATEGORIZED] += a if debit else -a
            if debit:
                spending.append((t, cat))
        elif bucket == "income":
            name = tree.get(cat["top_id"], cat).get("name") or "Other money in"
            money_in[name]["amount"] += -a if debit else a
            money_in[name]["count"] += 1
        elif bucket == "savings":
            if debit:
                investing.append((t, cat))
            key = "Investments" if debit else "From investments"
            (extra_out if debit else money_in)[key]["amount"] += a
            (extra_out if debit else money_in)[key]["count"] += 1
        else:
            _transfer(t, cat, account_types, covered, account_id, extra_out, money_in)

    # A category with more refunds than spending is money in, not negative spending.
    for cid in [cid for cid, r in out_cats.items() if r["amount"] < 0]:
        money_in["Refunds"]["amount"] += -out_cats.pop(cid)["amount"]

    months = _months_covered(txns, period, today)
    total_out = sum(r["amount"] for r in out_cats.values()) + sum(r["amount"] for r in extra_out.values())
    total_in = sum(r["amount"] for r in money_in.values())
    one_offs = _one_offs(spending, history)
    one_off_total = sum(o["amount_paise"] for o in one_offs)
    mixups = _mixups(spending + investing, seed_words, [n for n in own_names if n])
    unexplained = _unexplained(out_cats, extra_out, spending, mixups, total_out)

    def per_month(v):
        return rupees(v / months) if months > 1 else None

    rows = [{
        "kind": "category", "category_id": r["category_id"], "name": r["name"], "group": r["group"],
        "amount": rupees(r["amount"]), "count": r["count"], "per_month": per_month(r["amount"]),
        "share": round(r["amount"] / total_out, 4) if total_out else None,
        "unexplained": r["name"] in (PEOPLE_CATEGORY, UNCATEGORIZED, OTHER),
        "top_children": [{"name": n, "amount": rupees(v)} for n, v in sorted(r["children"].items(), key=lambda kv: -kv[1])
                         if v > 0 and n != r["name"]][:3],
    } for r in out_cats.values()]
    labels = {
        "Investments": ("investments", "Investments", False),
        "card_bills": ("card_bills", "Credit card bills (purchases not imported)", True),
        "to_cards": ("to_cards", "Paid to your credit cards", False),
        "own_accounts": ("own_accounts", "To your other accounts", False),
    }
    for key, r in extra_out.items():
        kind_, name, unknown = labels[key]
        rows.append({"kind": kind_, "category_id": None, "name": name, "group": None, "amount": rupees(r["amount"]),
                     "count": r["count"], "per_month": per_month(r["amount"]),
                     "share": round(r["amount"] / total_out, 4) if total_out else None, "unexplained": unknown, "top_children": []})
    rows = [r for r in rows if r["amount"]]
    rows.sort(key=lambda r: -r["amount"])

    left = total_in - total_out
    check = None
    if balance:
        gap = left - balance["change"]
        check = {"matches": abs(gap) <= max(1_000_00, 0.01 * max(total_out, 1)), "gap": rupees(gap)}

    return {
        "period": {"kind": period.kind, "label": period.label, "start": period.start.isoformat(), "end": period.end.isoformat(),
                   "in_progress": period.in_progress, "months": round(months, 2), "months_in_period": period.months},
        "money_out": rupees(total_out),
        "money_in": rupees(total_in),
        "left": rupees(left),
        "out_per_month": per_month(total_out),
        "in_per_month": per_month(total_in),
        "balance": {k: rupees(v) for k, v in balance.items()} if balance else None,
        "check": check,
        "rows": rows,
        "came_from": sorted(({"name": k, "amount": rupees(v["amount"]), "count": v["count"]}
                             for k, v in money_in.items() if v["amount"]), key=lambda r: -r["amount"]),
        "one_offs": [{k: v for k, v in o.items() if k != "amount_paise"} for o in one_offs],
        "one_off_total": rupees(one_off_total),
        "regular_out": rupees(total_out - one_off_total),
        "regular_per_month": per_month(total_out - one_off_total),
        "unexplained": unexplained,
    }


def _transfer(t, cat, account_types, covered, account_id, extra_out, money_in) -> None:
    """Transfers: internal ones (between accounts in view) are skipped; the rest is money in or out."""
    a, debit = t["amount_paise"], t["direction"] == "debit"
    on_card = account_types.get(t["account_id"]) == "credit_card"
    if cat.get("top_name") == CARD_PAYMENT:
        if on_card or not debit:
            return  # the card's record of a bill payment: what it paid for is counted as the card's purchases
        itemised = t["linked_txn_id"] is not None or any(s <= t["txn_date"] <= e for s, e in covered)
        if not itemised:
            key = "card_bills"
        elif account_id:
            key = "to_cards"  # looking at one bank account: the money left it for a card in view elsewhere
        else:
            return
        extra_out[key]["amount"] += a
        extra_out[key]["count"] += 1
        return
    if t["linked_txn_id"] is not None and not account_id:
        return  # both sides are in view
    target = extra_out["own_accounts"] if debit else money_in["From your other accounts"]
    target["amount"] += a
    target["count"] += 1


def _months_covered(txns: list[dict], period, today: date) -> float:
    months = {t["txn_date"][:7] for t in txns}
    n = float(len(months)) or 1.0
    current = today.strftime("%Y-%m")
    if period.in_progress and current in months:
        n -= 1 - today.day / calendar.monthrange(today.year, today.month)[1]
    return max(n, 1.0)


def _one_offs(spending: list[tuple[dict, dict]], history: list[dict]) -> list[dict]:
    """Large payments that aren't part of a pattern: a new payee, or far above what you usually pay them."""
    by_payee: dict[str, list[dict]] = defaultdict(list)
    for h in history:
        if h["direction"] == "debit":
            by_payee[h["merchant_key"]].append(h)
    # Regular = paid in 3 different months (fewer when there's less history to go on).
    needed = min(3, len({h["txn_date"][:7] for h in history}) or 1)
    out = []
    for t, cat in spending:
        if t["amount_paise"] < ONE_OFF_MIN:
            continue
        others = [h for h in by_payee[t["merchant_key"]] if h["id"] != t["id"]]
        regular_payee = len({h["txn_date"][:7] for h in others} | {t["txn_date"][:7]}) >= needed
        usual = statistics.median(h["amount_paise"] for h in others) if others else 0
        if regular_payee and (not usual or t["amount_paise"] <= 3 * usual):
            continue  # a regular payment, like rent
        out.append({
            "id": t["id"], "date": t["txn_date"], "payee": t["counterparty"] or t["merchant_key"].partition(":")[2],
            "category": cat.get("name"), "amount": rupees(t["amount_paise"]), "amount_paise": t["amount_paise"],
            "why": f"about {t['amount_paise'] / usual:.0f}× what you usually pay them" if regular_payee
                   else "a payee you don't pay regularly",
        })
    out.sort(key=lambda o: -o["amount_paise"])
    return out


def _unexplained(out_cats: dict, extra_out: dict, spending: list, mixups: list[dict], total_out: int) -> dict:
    items = []
    card = extra_out.get("card_bills")
    if card and card["amount"]:
        items.append({"kind": "card_bills", "amount": rupees(card["amount"]), "count": card["count"],
                      "text": "Credit card bills paid with no card statement imported, so what you bought is unknown.",
                      "fix": "Import your card statements"})
    by_name = {r["name"]: r for r in out_cats.values()}
    people = by_name.get(PEOPLE_CATEGORY)
    if people and people["amount"] > 0:
        pays = [t for t, cat in spending if cat.get("top_name") == PEOPLE_CATEGORY]
        big = [t for t in pays if t["amount_paise"] >= BIG_PEOPLE_PAYMENT]
        payees = len({t["merchant_key"] for t in pays})
        text = f"{len(pays)} payments to {payees} people. What they were for isn't known."
        if big:
            text += f" {len(big)} of them, of ₹5,000 or more, make up {round(100 * sum(t['amount_paise'] for t in big) / people['amount'])}%."
        items.append({"kind": "people", "category_id": people["category_id"], "amount": rupees(people["amount"]),
                      "count": len(pays), "big_count": len(big), "big_amount": rupees(sum(t["amount_paise"] for t in big)),
                      "text": text, "fix": "Say what the biggest ones were for"})
    for name, kind in ((UNCATEGORIZED, "uncategorized"), (OTHER, "other")):
        r = by_name.get(name)
        if r and r["amount"] > 0:
            items.append({"kind": kind, "category_id": r["category_id"], "amount": rupees(r["amount"]), "count": r["count"],
                          "text": "No category yet." if kind == "uncategorized" else "Filed as Other: a category says what it was.",
                          "fix": "Review them" if kind == "uncategorized" else "Pick categories"})
    if mixups:
        total = sum(m["amount_paise"] for m in mixups)
        items.append({"kind": "mixups", "amount": rupees(total), "count": len(mixups),
                      "text": "Payments that are probably in the wrong category.", "fix": "Check them",
                      "items": [{k: v for k, v in m.items() if k != "amount_paise"} for m in mixups]})
    total = sum(round(i["amount"] * 100) for i in items)
    return {"amount": rupees(total), "share": round(total / total_out, 4) if total_out else None, "items": items}


def _mixups(txns: list[tuple[dict, dict]], seed_words: dict[int, str], own_names: list[set[str]]) -> list[dict]:
    """Payments that are probably in the wrong category.

    * A payment to a person that a built-in keyword put somewhere else because the word was in the
      note ("RENT PLUS PARKING" -> Parking & Tolls), not in the payee's name (Swiggy's own UPI ID is fine).
    * A "bank charge" of thousands of rupees.
    """
    out = []
    for t, cat in txns:
        name, top = cat.get("name"), cat.get("top_name")
        payee = t["counterparty"] or ""
        why = None
        word = seed_words.get(t["rule_id"]) if t["category_source"] == "seed" else None
        if (word and t["amount_paise"] >= MIXUP_MIN and top not in _PERSON_OK and name not in _PERSON_OK
                and word not in payee.upper() and word not in (t["vpa"] or "").upper()
                and looks_like_person(payee, t["vpa"], t["channel"])):
            words = set(payee.upper().split())
            if any(n <= words for n in own_names):
                why = f"sent to your own name, filed as {name} because the note says {word}: a transfer if it went to your own account"
            else:
                why = f"a payment to a person, filed as {name} because the note says {word}"
        elif top == "Bank Charges" and t["amount_paise"] >= BIG_BANK_CHARGE:
            why = "very large for a bank charge"
        if why:
            out.append({"id": t["id"], "date": t["txn_date"], "payee": payee or t["merchant_key"].partition(":")[2],
                        "category": name, "amount": rupees(t["amount_paise"]), "amount_paise": t["amount_paise"], "why": why})
    out.sort(key=lambda m: -m["amount_paise"])
    return out


def _balance_change(conn, start: date, end: date, account_id: int | None) -> dict | None:
    """Opening and closing balance over the period, summed over bank accounts that print a running balance."""
    sql = "SELECT id FROM accounts WHERE type != 'credit_card'" + (" AND id = ?" if account_id else "")
    opening = closing = found = 0
    for (acct,) in conn.execute(sql, (account_id,) if account_id else ()).fetchall():
        rng = (acct, start.isoformat(), end.isoformat())
        first = conn.execute(
            """SELECT balance_paise, amount_paise, direction FROM transactions WHERE account_id = ? AND txn_date BETWEEN ? AND ?
               AND balance_paise IS NOT NULL ORDER BY txn_date, id LIMIT 1""", rng).fetchone()
        last = conn.execute(
            """SELECT balance_paise FROM transactions WHERE account_id = ? AND txn_date BETWEEN ? AND ?
               AND balance_paise IS NOT NULL ORDER BY txn_date DESC, id DESC LIMIT 1""", rng).fetchone()
        if not first or not last:
            continue
        opening += first["balance_paise"] + (first["amount_paise"] if first["direction"] == "debit" else -first["amount_paise"])
        closing += last["balance_paise"]
        found += 1
    return {"opening": opening, "closing": closing, "change": closing - opening} if found else None
