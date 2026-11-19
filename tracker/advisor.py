"""Money plan: budgeting and spending guidance from your own numbers.

Claude gets a snapshot of *totals only*: monthly income and spending per category, savings
rate, recurring commitments and the biggest merchants. Names of individuals, individual
transactions and account numbers are never included. If Claude isn't reachable, a plan is
built from simple rules (50/30/20 reference, trimming the largest discretionary categories).

This is general budgeting guidance, not professional financial advice. The plan deliberately
avoids recommending specific investment products, funds, stocks, insurance policies or loans.
"""

import json
import logging
import math
import re
from datetime import date

import config
from tracker import db, insights, local_classifier
from tracker.local_classifier import looks_like_person

log = logging.getLogger(__name__)

DISCLAIMER = ("General budgeting guidance based only on your transactions. Not professional financial advice; "
              "for investment, insurance or tax decisions, talk to a SEBI-registered investment adviser.")

SYSTEM_PROMPT = """You are a practical, encouraging personal-finance coach for one person in India.
You get a JSON snapshot of their own bank data: monthly totals by category (sub-categories nested),
income, savings rate, recurring commitments and their biggest merchants. Amounts are Indian rupees.

Write a short, specific money plan:
- Base everything on the numbers given. Name categories and amounts; don't invent spending that isn't there.
- Prioritise the few changes with the biggest monthly impact. Be realistic: don't suggest cutting rent,
  EMIs, insurance, utilities or other essentials unless something stands out as unusual.
- Use 50/30/20 (needs/wants/savings) as a reference point, not a rule; adapt targets to their situation.
- Suggest an emergency fund target of roughly 6 months of essential spending, and how to build it.
- Suggest monthly budgets only for the main categories listed in the snapshot, close to their recent
  averages, trimmed where the plan says to cut.
- Do NOT recommend specific investment products, funds, stocks, insurance policies, loans or tax schemes,
  and don't predict returns. You may say "invest regularly" or "review your insurance cover" and suggest
  speaking to a SEBI-registered adviser for those decisions.
- Keep it brief: a 2-3 sentence summary and 3-6 actions. Plain language, no jargon."""


def _schema(categories: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "health": {
                "type": "object",
                "properties": {"rating": {"type": "string", "enum": ["strong", "okay", "needs_attention"]}, "comment": {"type": "string"}},
                "required": ["rating", "comment"],
                "additionalProperties": False,
            },
            "targets": {
                "type": "object",
                "properties": {
                    "needs_pct": {"type": "number"}, "wants_pct": {"type": "number"}, "savings_pct": {"type": "number"},
                    "note": {"type": "string"},
                },
                "required": ["needs_pct", "wants_pct", "savings_pct", "note"],
                "additionalProperties": False,
            },
            "actions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "detail": {"type": "string"},
                        "category": {"type": "string"},
                        "monthly_saving": {"type": "number"},
                        "priority": {"type": "string", "enum": ["high", "medium", "low"]},
                    },
                    "required": ["title", "detail", "category", "monthly_saving", "priority"],
                    "additionalProperties": False,
                },
            },
            "budgets": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "category": {"type": "string", "enum": categories},
                        "monthly": {"type": "number"},
                        "reason": {"type": "string"},
                    },
                    "required": ["category", "monthly", "reason"],
                    "additionalProperties": False,
                },
            },
            "emergency_fund": {"type": "string"},
            "watch": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["summary", "health", "targets", "actions", "budgets", "emergency_fund", "watch"],
        "additionalProperties": False,
    }


# ── Snapshot (what Claude sees) ───────────────────────────────────────────────


_BUSINESS_WORDS = (local_classifier._BUSINESS_WORDS | local_classifier._LEXICON_WORDS
                   | {"LTD", "LIMITED", "PVT", "PRIVATE", "TECHNOLOGIES", "TECHNOLOGY", "SOLUTIONS", "FUND", "INSURANCE", "FINANCE"})
_KNOWN_MERCHANTS = [re.compile(rf"(?<![A-Z]){re.escape(kw)}S?(?![A-Z])") for _, _, kws in config.SEED_RULES for kw in kws if len(kw) >= 3]


def is_business(name: str) -> bool:
    up = name.upper()
    return bool(set(re.findall(r"[A-Z]+", up)) & _BUSINESS_WORDS) or any(p.search(up) for p in _KNOWN_MERCHANTS)


def _safe_name(name: str | None, category: str | None) -> str:
    """Share a payee's name only when it's clearly a business; anyone else is just 'a person'."""
    if name and category != insights.PEOPLE_CATEGORY and is_business(name) and not looks_like_person(name, None, "UPI"):
        return name
    return f"a person ({category})" if category else "a person"


def build_snapshot(months_back: int = 3) -> dict:
    with db.session() as conn:
        latest = insights.latest_month(conn)
    today_month = date.today().strftime("%Y-%m")
    complete = [insights._shift(latest, -i) for i in range(months_back)] if latest < today_month else \
               [insights._shift(latest, -i) for i in range(1, months_back + 1)]
    months = []
    for m in sorted(complete):
        o = insights.overview("month", m)
        if not (o["income"] or o["spent"]):
            continue
        months.append({
            "month": m,
            "income": o["income"], "spent": o["spent"], "needs": o["needs"], "wants": o["wants"],
            "invested": o["invested"], "saved": o["saved"], "savings_rate": o["savings_rate"],
            "categories": [
                {"name": c["name"], "group": c["group"], "amount": c["amount"],
                 "sub": [{"name": s["name"], "amount": s["amount"], "count": s["count"]} for s in c["children"] if not s["is_parent"]]}
                for c in o["categories"]
            ],
        })
    latest_o = insights.overview("month", latest)
    n = max(len(months), 1)
    avg = lambda key: round(sum(m[key] or 0 for m in months) / n, 2)  # noqa: E731
    cat_avg: dict[str, float] = {}
    for m in months:
        for c in m["categories"]:
            cat_avg[c["name"]] = cat_avg.get(c["name"], 0) + c["amount"] / n
    three = insights.overview("3m", complete[0] if complete else latest)
    with db.session() as conn:
        budgets = {r["name"]: r["monthly_paise"] / 100 for r in conn.execute(
            "SELECT c.name, b.monthly_paise FROM budgets b JOIN categories c ON c.id = b.category_id")}
    return {
        "currency": "INR",
        "months": months,
        "averages": {"income": avg("income"), "spent": avg("spent"), "needs": avg("needs"), "wants": avg("wants"),
                     "invested": avg("invested"), "saved": avg("saved")},
        "category_monthly_average": {k: round(v, 2) for k, v in sorted(cat_avg.items(), key=lambda kv: -kv[1])},
        "current_month": {"month": latest, "in_progress": latest_o["period"]["in_progress"],
                          "spent_so_far": latest_o["spent"], "income_so_far": latest_o["income"]},
        "recurring_commitments": [
            {"payee": _safe_name(r["payee"], r["category"]), "category": r["category"], "monthly": r["monthly"]}
            for r in latest_o["recurring"] if r["active"]
        ],
        "recurring_monthly_total": latest_o["recurring_monthly"],
        "top_merchants_last_3_months": [
            {"payee": _safe_name(mm["name"], mm["category"]), "category": mm["category"], "total": mm["amount"], "visits": mm["count"]}
            for mm in three["top_merchants"][:10]
        ],
        "payments_to_individuals_last_3_months": three["people_total"],
        "current_budgets": budgets,
    }


# ── Plans ─────────────────────────────────────────────────────────────────────


def create_plan(use_claude: bool = True) -> dict:
    snapshot = build_snapshot()
    if not snapshot["months"]:
        raise ValueError("Import at least one full month of statements first.")
    categories = list(snapshot["category_monthly_average"]) or ["Other"]
    plan, backend, errors = None, None, []
    if use_claude:
        from tracker import llm

        with db.session() as conn:
            mode = db.get_setting(conn, "categorizer", "auto")
        if mode != "local":
            user = "My spending snapshot:\n" + json.dumps(snapshot, ensure_ascii=False, indent=1)
            plan, backend, errors = llm.structured_completion(SYSTEM_PROMPT, user, _schema(categories), config.PLAN_MODEL, mode=mode)
    if plan is None:
        plan, backend = offline_plan(snapshot), "Offline rules"
    plan["budgets"] = [b for b in plan.get("budgets", []) if b.get("category") in categories and b.get("monthly", 0) > 0]
    record = {
        "plan": plan,
        "backend": backend,
        "errors": errors,
        "based_on": [m["month"] for m in snapshot["months"]],
        "averages": snapshot["averages"],
        "disclaimer": DISCLAIMER,
    }
    with db.session() as conn:
        cur = conn.execute("INSERT INTO money_plans (backend, plan_json) VALUES (?, ?)", (backend, json.dumps(record)))
        record["id"] = cur.lastrowid
        record["created_at"] = conn.execute("SELECT created_at FROM money_plans WHERE id = ?", (cur.lastrowid,)).fetchone()[0]
    log.info("Money plan %s created by %s from %d months: %d actions, %d budgets%s", record["id"], backend,
             len(snapshot["months"]), len(plan.get("actions", [])), len(plan["budgets"]),
             f" (fell back after: {'; '.join(errors)})" if errors and backend == "Offline rules" else "")
    return record


def latest_plan() -> dict | None:
    with db.session() as conn:
        row = conn.execute("SELECT id, created_at, plan_json FROM money_plans ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return None
    record = json.loads(row["plan_json"])
    record.update(id=row["id"], created_at=row["created_at"])
    return record


def apply_budgets(plan_id: int) -> list[dict]:
    from tracker import service

    with db.session() as conn:
        row = conn.execute("SELECT plan_json FROM money_plans WHERE id = ?", (plan_id,)).fetchone()
    if not row:
        raise service.NotFound(f"Plan {plan_id} not found")
    out = []
    for b in json.loads(row["plan_json"])["plan"].get("budgets", []):
        try:
            out.append(service.set_budget(b["category"], round(b["monthly"], -1)))
        except (ValueError, LookupError):
            continue  # category renamed or removed since the plan was made
    log.info("Applied %d budgets from money plan %s", len(out), plan_id)
    return out


def _round_up(v: float, step: int = 100) -> float:
    return float(math.ceil(v / step) * step)


def offline_plan(s: dict) -> dict:
    """Rule-based plan when Claude isn't available."""
    a = s["averages"]
    income, needs, wants, saved = a["income"], a["needs"], a["wants"], a["saved"]
    rate = saved / income if income else None
    cats = s["category_monthly_average"]
    groups = {c["name"]: c["group"] for m in s["months"] for c in m["categories"]}
    actions = []

    if income and rate is not None and rate < 0.2:
        gap = 0.2 * income - saved
        actions.append({"title": "Work up to saving 20% of your income", "priority": "high", "category": "",
                        "monthly_saving": round(gap, -2),
                        "detail": f"You keep about {rate:.0%} of your income. Saving 20% means about ₹{gap:,.0f} more each month; "
                                  "the steps below show where it can come from."})
    wants_cats = sorted(((n, v) for n, v in cats.items() if groups.get(n) == "wants" and n not in ("Uncategorized", insights.PEOPLE_CATEGORY)),
                        key=lambda kv: -kv[1])
    for name, v in wants_cats[:2]:
        if v < 1000:
            continue
        cut = round(v * 0.15, -2)
        actions.append({"title": f"Trim {name} by about 15%", "priority": "medium", "category": name, "monthly_saving": cut,
                        "detail": f"You spend about ₹{v:,.0f} a month here. A 15% cut saves about ₹{cut:,.0f} a month."})
    for m in s["months"][-1:]:
        for c in m["categories"]:
            for sub in c["sub"]:
                if sub["name"] == "Food Delivery" and sub["amount"] >= 3000:
                    actions.append({"title": "Set a weekly limit for food delivery", "priority": "medium", "category": c["name"],
                                    "monthly_saving": round(sub["amount"] * 0.3, -2),
                                    "detail": f"{sub['count']} orders came to ₹{sub['amount']:,.0f} last month. "
                                              "Cooking a few more meals a week could save about 30% of that."})
    subs = [r for r in s["recurring_commitments"] if r["category"] in ("OTT & Music", "Apps & Games", "Entertainment & Subscriptions", "Fitness")]
    if subs:
        total = sum(r["monthly"] for r in subs)
        actions.append({"title": "Review your subscriptions", "priority": "low", "category": "Entertainment & Subscriptions",
                        "monthly_saving": round(total * 0.3, -1),
                        "detail": f"{len(subs)} subscriptions cost about ₹{total:,.0f} a month. Cancel the ones you rarely use."})
    cash = cats.get("Cash Withdrawal", 0)
    if a["spent"] and cash / a["spent"] >= 0.1:
        actions.append({"title": "Track cash spending", "priority": "low", "category": "Cash Withdrawal", "monthly_saving": 0,
                        "detail": f"About ₹{cash:,.0f} a month goes out as cash, which this app can't see. Prefer UPI or note cash spends."})

    budgets = []
    for name, v in cats.items():
        if v < 500 or name == "Uncategorized":
            continue
        target = v * (0.9 if groups.get(name) == "wants" else 1.0)
        budgets.append({"category": name, "monthly": _round_up(target), "reason":
                        "Your recent average, trimmed 10%." if groups.get(name) == "wants" else "Your recent average."})

    efund = 6 * needs
    return {
        "summary": (f"Over the last {len(s['months'])} month(s) you earned about ₹{income:,.0f} a month and spent ₹{a['spent']:,.0f}"
                    + (f", keeping {rate:.0%}." if rate is not None else ".")
                    + " Focus on the actions below, starting with the highest impact."),
        "health": {"rating": "strong" if rate is not None and rate >= 0.2 else ("okay" if rate is not None and rate >= 0.1 else "needs_attention"),
                   "comment": "Based on how much of your income you keep each month."},
        "targets": {"needs_pct": 50, "wants_pct": 30, "savings_pct": 20,
                    "note": f"Now: needs {needs / income:.0%}, wants {wants / income:.0%}, saved {rate:.0%}." if income else "No income recorded yet."},
        "actions": actions[:6],
        "budgets": budgets,
        "emergency_fund": f"Aim for about ₹{efund:,.0f}: six months of your essential spending (₹{needs:,.0f} a month). "
                          "Build it gradually by setting a fixed amount aside each month in a separate account.",
        "watch": [f"{r['payee']}: ₹{r['monthly']:,.0f}/month ({r['category']})" for r in s["recurring_commitments"][:5]],
    }
