"""The learning loop: seeds, Claude cache, corrections, and how they propagate."""

from conftest import FakeClassifier
from helpers import MONTH1, MONTH2, hdfc_xlsx

from tracker import db, service
from tracker.importer import import_file


def _txns(**filters):
    return {t["narration_raw"][:40]: t for t in service.list_transactions(limit=1000, **filters)["items"]}


def _by_counterparty(name):
    return [t for t in service.list_transactions(limit=1000)["items"] if t["counterparty"] == name]


def _account():
    return service.create_account(name="HDFC Savings", bank="HDFC", last4="1234", holder_name="Test User")


def test_seed_rules_categorize_known_merchants_without_claude(tmp_path, fake_llm):
    _account()
    import_file(hdfc_xlsx(tmp_path / "apr.xlsx", MONTH1), classifier=fake_llm)
    swiggy = _by_counterparty("Swiggy")
    assert len(swiggy) == 2
    assert all(t["category"] == "Food & Dining" and t["category_source"] == "seed" for t in swiggy)
    atm = _by_counterparty("ATM withdrawal")[0]
    assert atm["category"] == "Cash Withdrawal"
    assert "SWIGGY" not in fake_llm.payees_seen  # seeds never reach the model


def test_unknown_merchants_go_to_claude_once_and_are_cached(tmp_path, fake_llm):
    _account()
    r1 = import_file(hdfc_xlsx(tmp_path / "apr.xlsx", MONTH1), classifier=fake_llm)
    assert r1.categorize["llm_calls"] == 1
    assert set(fake_llm.payees_seen) >= {"KUMAR STORES", "RAMESH KUMAR", "SRI MURUGAN TRADERS"}
    kumar = _by_counterparty("Kumar Stores")[0]
    assert (kumar["category"], kumar["category_source"]) == ("Groceries", "llm")

    seen_before = len(fake_llm.payees_seen)
    import_file(hdfc_xlsx(tmp_path / "may.xlsx", MONTH2), classifier=fake_llm)
    new_payees = fake_llm.payees_seen[seen_before:]
    assert new_payees == ["GREENLEAF ENTERPRISES"]  # Kumar Stores / Ramesh came from the cache
    may_kumar = [t for t in _by_counterparty("Kumar Stores") if t["txn_date"].startswith("2024-05")][0]
    assert (may_kumar["category"], may_kumar["category_source"]) == ("Groceries", "llm")


def test_low_confidence_is_flagged_for_review(tmp_path, fake_llm):
    _account()
    import_file(hdfc_xlsx(tmp_path / "apr.xlsx", MONTH1), classifier=fake_llm)
    murugan = _by_counterparty("Sri Murugan Traders")[0]
    assert murugan["category"] == "Shopping" and murugan["needs_review"]


def test_claude_failure_falls_back_to_offline_guesses_and_retry_upgrades_them(tmp_path, fake_llm):
    _account()
    broken = FakeClassifier(error="network down")
    result = import_file(hdfc_xlsx(tmp_path / "apr.xlsx", MONTH1), classifier=broken)
    assert result.categorize["llm_error"] == "network down"
    assert result.categorize["local_guesses"] >= 2
    kumar = _by_counterparty("Kumar Stores")[0]
    assert (kumar["category"], kumar["category_source"]) == ("Groceries", "local")  # "STORES" -> Groceries
    rent = _by_counterparty("Ramesh Kumar")[0]
    assert (rent["category"], rent["category_source"]) == ("Rent", "local")  # UPI remark "RENT"

    # Offline guesses aren't cached, so Claude gets them on the next re-check.
    service.recategorize("uncategorized", classifier=fake_llm)
    kumar = _by_counterparty("Kumar Stores")[0]
    assert (kumar["category"], kumar["category_source"]) == ("Groceries", "llm")


def test_offline_only_when_no_classifier(tmp_path):
    _account()
    with db.session() as conn:
        db.set_setting(conn, "categorizer", "local")
    result = import_file(hdfc_xlsx(tmp_path / "apr.xlsx", MONTH1), classifier=None)
    assert result.categorize["llm_calls"] == 0
    assert result.categorize["by_source"].get("local", 0) >= 2


def test_merchant_correction_updates_past_and_future(tmp_path, fake_llm):
    _account()
    import_file(hdfc_xlsx(tmp_path / "apr.xlsx", MONTH1), classifier=fake_llm)
    rent = _by_counterparty("Ramesh Kumar")[0]
    assert rent["category"] == "Payments to People"

    out = service.set_category(rent["id"], "Rent", scope="merchant")
    assert out["updated"] == 1

    seen_before = len(fake_llm.payees_seen)
    import_file(hdfc_xlsx(tmp_path / "may.xlsx", MONTH2), classifier=fake_llm)
    assert "RAMESH KUMAR" not in fake_llm.payees_seen[seen_before:]
    both = _by_counterparty("Ramesh Kumar")
    assert len(both) == 2
    assert all(t["category"] == "Rent" and t["category_source"] == "rule" for t in both)


def test_correction_applies_to_all_existing_similar_transactions(tmp_path, fake_llm):
    _account()
    import_file(hdfc_xlsx(tmp_path / "apr.xlsx", MONTH1), classifier=fake_llm)
    import_file(hdfc_xlsx(tmp_path / "may.xlsx", MONTH2), classifier=fake_llm)
    kumar = _by_counterparty("Kumar Stores")
    assert len(kumar) == 2
    out = service.set_category(kumar[0]["id"], "Household & Services")
    assert out["updated"] == 2
    assert {t["category"] for t in _by_counterparty("Kumar Stores")} == {"Household & Services"}


def test_user_rule_beats_seed_rule(tmp_path, fake_llm):
    _account()
    import_file(hdfc_xlsx(tmp_path / "apr.xlsx", MONTH1), classifier=fake_llm)
    swiggy = _by_counterparty("Swiggy")[0]
    service.set_category(swiggy["id"], "Groceries")
    assert {t["category"] for t in _by_counterparty("Swiggy")} == {"Groceries"}


def test_single_scope_is_a_one_off_that_later_rules_respect(tmp_path, fake_llm):
    _account()
    import_file(hdfc_xlsx(tmp_path / "apr.xlsx", MONTH1), classifier=fake_llm)
    import_file(hdfc_xlsx(tmp_path / "may.xlsx", MONTH2), classifier=fake_llm)
    april, may = sorted(_by_counterparty("Ramesh Kumar"), key=lambda t: t["txn_date"])
    service.set_category(april["id"], "Gifts & Donations", scope="single")
    service.set_category(may["id"], "Rent", scope="merchant")
    april_after = service.get_transaction(april["id"])
    assert (april_after["category"], april_after["category_source"]) == ("Gifts & Donations", "manual")
    assert service.get_transaction(may["id"])["category"] == "Rent"


def test_corrections_become_few_shot_examples(tmp_path, fake_llm):
    _account()
    import_file(hdfc_xlsx(tmp_path / "apr.xlsx", MONTH1), classifier=fake_llm)
    service.set_category(_by_counterparty("Ramesh Kumar")[0]["id"], "Rent")
    import_file(hdfc_xlsx(tmp_path / "may.xlsx", MONTH2), classifier=fake_llm)
    assert ("debit | RAMESH KUMAR", "Rent") in fake_llm.calls[-1]["examples"]


def test_accept_promotes_claude_pick_to_user_rule(tmp_path, fake_llm):
    _account()
    import_file(hdfc_xlsx(tmp_path / "apr.xlsx", MONTH1), classifier=fake_llm)
    murugan = _by_counterparty("Sri Murugan Traders")[0]
    service.accept_categories([murugan["id"]])
    after = service.get_transaction(murugan["id"])
    assert after["category_source"] == "rule" and not after["needs_review"]
    rules = service.list_rules(source="user")
    assert any(r["pattern"] == "D:SRI MURUGAN TRADERS" for r in rules)


def test_contains_rule_and_delete_rule(tmp_path, fake_llm):
    _account()
    import_file(hdfc_xlsx(tmp_path / "apr.xlsx", MONTH1), classifier=fake_llm)
    out = service.create_rule("murugan", "Groceries", match_type="contains", direction="debit")
    assert out["matched_transactions"] == 1
    assert _by_counterparty("Sri Murugan Traders")[0]["category"] == "Groceries"

    service.delete_rule(out["rule"]["id"], classifier=fake_llm)
    # Falls back to the cached Claude answer
    assert _by_counterparty("Sri Murugan Traders")[0]["category"] == "Shopping"


def test_merge_and_rename_categories(tmp_path, fake_llm):
    _account()
    import_file(hdfc_xlsx(tmp_path / "apr.xlsx", MONTH1), classifier=fake_llm)
    with db.session() as conn:
        food = db.category_id_by_name(conn, "Food & Dining")
        groceries = db.category_id_by_name(conn, "Groceries")
    service.update_category(food, name="Eating Out")
    assert _by_counterparty("Swiggy")[0]["category"] == "Eating Out"
    res = service.merge_categories(food, groceries)
    assert res["moved_transactions"] == 2
    assert _by_counterparty("Swiggy")[0]["category"] == "Groceries"
