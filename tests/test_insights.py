"""Statistics, sub-categories, budgets and the money plan, on four synthetic months."""

import json
from datetime import date

import pytest
from helpers import hdfc_xlsx

from tracker import advisor, db, insights, llm, service
from tracker.importer import import_file

MONTHS = ["2026-07", "2026-08", "2026-09", "2026-10"]


def _rows(month: str, i: int, big_purchase: bool = False) -> list[tuple]:
    y, m = map(int, month.split("-"))
    d = lambda day: date(y, m, day).strftime("%d/%m/%y")  # noqa: E731
    r = lambda n: f"6{m:02d}{n:09d}"  # noqa: E731
    rows = [
        (d(1), f"NEFT CR-SBIN0001234-ACME TECHNOLOGIES PVT LTD-TEST USER-SBINN5{r(1)}", r(1), None, 100000.00),
        (d(3), f"UPI-RAMESH KUMAR-9876543210@YBL-SBIN0001234-{r(2)}-RENT", r(2), 20000.00, None),
        (d(5), f"ACH D- INDIAN CLEARING CORP-{r(3)}", r(3), 5000.00, None),
        (d(6), "ME DC SI 416021XXXXXX1234 NETFLIX", r(4), 649.00, None),
        (d(8), f"UPI-KUMAR STORES-KUMARSTORES@OKSBI-SBIN0001234-{r(5)}-UPI", r(5), 3000.00 + 100 * i, None),
        (d(12), f"UPI-UBER INDIA SYSTEMS-UBER@AXISBANK-UTIB0000001-{r(6)}-UPI", r(6), 400.00, None),
    ]
    rows += [(d(10 + k), f"UPI-SWIGGY-SWIGGY@ICICI-ICIC0DC0099-{r(20 + k)}-UPI", r(20 + k), 300.00, None) for k in range(4 + 2 * i)]
    if big_purchase:
        rows.append((d(20), f"UPI-AMAZON-AMAZON@APL-UTIB0000001-{r(40)}-UPI", r(40), 18000.00, None))
    bal, out = 500000.0, []
    for dt, narr, ref, wd, dep in sorted(rows, key=lambda x: x[0]):
        bal += (dep or 0) - (wd or 0)
        out.append((dt, narr, ref, wd, dep, round(bal, 2)))
    return out


@pytest.fixture
def history(tmp_path, fake_llm):
    service.create_account(name="HDFC Savings", bank="HDFC", last4="1234", holder_name="Test User")
    for i, m in enumerate(MONTHS):
        import_file(hdfc_xlsx(tmp_path / f"{m}.xlsx", _rows(m, i, big_purchase=(m == MONTHS[-1]))), classifier=fake_llm)
    # You tell the app the person you pay on the 3rd is your landlord.
    rent = service.list_transactions(q="RAMESH")["items"][0]
    service.set_category(rent["id"], "Rent")


def test_sub_categories_roll_up(history):
    o = insights.overview("month", "2026-10")
    food = next(c for c in o["categories"] if c["name"] == "Food & Dining")
    assert [ch["name"] for ch in food["children"]] == ["Food Delivery"]
    assert food["amount"] == 10 * 300.0 and food["count"] == 10
    swiggy = service.list_transactions(category="Food & Dining", month="2026-10")["total"]
    assert swiggy == 10  # filtering by a main category includes its sub-categories


def test_savings_rate_needs_wants_and_investments(history):
    o = insights.overview("month", "2026-10")
    assert o["income"] == 100000.0
    assert o["invested"] == 5000.0  # SIP counted as investment, not spending
    needs = 20000 + 3300 + 400  # rent, groceries, transport
    wants = 649 + 3000 + 18000  # Netflix, food delivery, the big Amazon order
    assert (o["needs"], o["wants"]) == (needs, wants)
    assert o["saved"] == 100000 - needs - wants
    assert o["savings_rate"] == round((100000 - needs - wants) / 100000, 3)


def test_change_vs_usual_and_unusual_payment(history):
    o = insights.overview("month", "2026-10")
    shopping = next(c for c in o["categories"] if c["name"] == "Shopping")
    assert shopping["usual"] is None  # never bought anything there before
    food = next(c for c in o["categories"] if c["name"] == "Food & Dining")
    assert food["usual"] == round((4 + 6 + 8) * 300 / 3, 2) and food["change_pct"] > 0.5
    assert o["unusual"][0]["payee"] == "Amazon" and o["unusual"][0]["amount"] == 18000.0
    texts = " ".join(f["text"] for f in o["facts"])
    assert "Food & Dining" in texts and "above your usual" in texts
    assert "food delivery orders" in texts


def test_recurring_commitments(history):
    o = insights.overview("month", "2026-10")
    names = {r["payee"]: r for r in o["recurring"]}
    assert {"Ramesh Kumar", "Indian Clearing Corp", "Netflix"} <= set(names)
    assert "Swiggy" not in names  # frequent purchases are habits, not commitments
    assert names["Ramesh Kumar"]["monthly"] == 20000.0
    assert o["recurring_monthly"] >= 20000 + 5000 + 649


def test_periods():
    assert insights.resolve_period("fy", "2026-02").label == "FY 2025-26"
    p = insights.resolve_period("fy", "2026-04")
    assert (p.start, p.end) == (date(2026, 4, 1), date(2027, 3, 31))
    assert insights.resolve_period("3m", "2026-10").start == date(2026, 8, 1)
    assert insights._inr(1234567) == "₹12,34,567"


def test_snapshot_hides_people(history):
    s = advisor.build_snapshot()
    blob = json.dumps(s)
    assert "RAMESH" not in blob.upper()  # the landlord is a person: never named
    assert "a person (Rent)" in blob
    assert "Swiggy" in blob or "SWIGGY" in blob.upper()  # businesses are fine
    assert [m["month"] for m in s["months"]] == ["2026-08", "2026-09", "2026-10"]


def test_offline_plan_and_budgets(history):
    rec = advisor.create_plan(use_claude=False)
    assert rec["backend"] == "Offline rules"
    plan = rec["plan"]
    assert plan["health"]["rating"] in ("strong", "okay", "needs_attention")
    assert any(b["category"] == "Food & Dining" for b in plan["budgets"])
    assert advisor.latest_plan()["id"] == rec["id"]
    applied = advisor.apply_budgets(rec["id"])
    assert applied and service.list_budgets()
    o = insights.overview("month", "2026-10")
    food = next(c for c in o["categories"] if c["name"] == "Food & Dining")
    assert food["budget"] and food["budget_used"] is not None


def test_claude_plan_uses_snapshot_and_falls_back(history, monkeypatch):
    seen = {}

    def fake(system, user, schema, model, mode="auto", **kw):
        seen.update(user=user, model=model, enum=schema["properties"]["budgets"]["items"]["properties"]["category"]["enum"])
        return {"summary": "Fine.", "health": {"rating": "okay", "comment": "c"},
                "targets": {"needs_pct": 50, "wants_pct": 30, "savings_pct": 20, "note": "n"},
                "actions": [{"title": "Cook more", "detail": "d", "category": "Food & Dining", "monthly_saving": 900, "priority": "high"}],
                "budgets": [{"category": "Food & Dining", "monthly": 2500, "reason": "r"}, {"category": "Made Up", "monthly": 1, "reason": "x"}],
                "emergency_fund": "e", "watch": []}, "Claude (your subscription)", []

    monkeypatch.setattr(llm, "structured_completion", fake)
    rec = advisor.create_plan()
    assert rec["backend"] == "Claude (your subscription)"
    assert seen["model"] == "claude-opus-5-5"
    assert "RAMESH" not in seen["user"].upper()
    assert [b["category"] for b in rec["plan"]["budgets"]] == ["Food & Dining"]  # unknown categories dropped

    monkeypatch.setattr(llm, "structured_completion", lambda *a, **k: (None, None, ["Claude API: no API credits left"]))
    rec = advisor.create_plan()
    assert rec["backend"] == "Offline rules" and rec["errors"]


def test_category_rules():
    with db.session() as conn:
        food = db.category_id_by_name(conn, "Food & Dining")
        delivery = db.category_id_by_name(conn, "Food Delivery")
    with pytest.raises(ValueError):
        service.set_budget(delivery, 1000)  # budgets live on main categories
    with pytest.raises(ValueError):
        service.update_category(food, parent_id=delivery)  # no grandchildren / has children
    sub = service.create_category("Office Lunch", parent_id=food)
    assert sub["grp"] == "wants" and sub["kind"] == "expense"
    service.update_category(food, grp="needs")
    with db.session() as conn:
        assert conn.execute("SELECT grp FROM categories WHERE id = ?", (sub["id"],)).fetchone()[0] == "needs"
