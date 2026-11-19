"""Assisted net banking against a fake bank: capture downloads, record steps, replay with a new date range.

The "user" is simulated with session.call(); the real app leaves login entirely to you.
Runs headless Edge/Chrome; skipped if neither can be launched.
"""

import json
import time
from datetime import date

import pytest

pytest.importorskip("playwright")

from fake_bank import FakeBank  # noqa: E402

from tracker import db, service  # noqa: E402
from tracker.fetch import netbanking  # noqa: E402

SECRET_PASSWORD = "s3cret-Passw0rd"
SECRET_USER = "cust77001"
SECRET_OTP = "482913"


@pytest.fixture
def bank():
    with FakeBank() as b:
        yield b


@pytest.fixture
def account(fake_llm, monkeypatch):
    monkeypatch.setattr("tracker.categorizer.default_classifier", lambda *a, **k: fake_llm)
    return service.create_account(name="HDFC Savings", bank="HDFC", last4="1234")


def _open(account, url) -> netbanking.NetBankingSession:
    s = netbanking.NetBankingSession(account, url, headless=True).start()
    deadline = time.time() + 60
    while s.state == "starting" and time.time() < deadline:
        time.sleep(0.2)
    if s.state == "error":
        pytest.skip(f"No browser available: {s.message}")
    return s


def _login(page):
    page.fill("#userid", SECRET_USER)
    page.fill("#password", SECRET_PASSWORD)
    page.fill("#otp", SECRET_OTP)
    page.click("#login")
    page.wait_for_url("**/home.html")


def _wait_imports(s, n, timeout=40):
    deadline = time.time() + timeout
    while len(s.snapshot()["imports"]) < n and time.time() < deadline:
        time.sleep(0.3)
    return s.snapshot()["imports"]


def _close(s):
    s.command("close").result(20)
    s._thread.join(20)


def test_record_then_replay(bank, account):
    # First visit: you log in, record, and download by hand.
    s = _open(account, f"{bank.base}/login.html")
    try:
        s.call(_login).result(30)
        s.command("record_start").result(10)

        def navigate(page):
            page.frame(name="left_menu").click("text=Account Statement")
            main = page.frame(name="main_part")
            main.wait_for_url("**/statement.html")
            main.select_option("#selAcct", "A1")
            main.get_by_label("Statement period").select_option(label="October 2026")
            main.get_by_label("From date").fill("01/10/2026")
            main.get_by_label("From date").press("Tab")
            main.click("#cal a")  # calendar pop-up sets the end date by script
            main.select_option("#fmt", "XLS")
            main.click("#btnDownload")  # the page then clicks a hidden <a> itself to download

        s.call(navigate).result(30)
        imports = _wait_imports(s, 1)
        assert imports and imports[0]["ok"], imports
        assert imports[0]["n_new"] == 2
        time.sleep(1.5)
        assert len(s.snapshot()["imports"]) == 1  # one download, handled once
        assert s.snapshot()["state"] == "open" and not s.recording  # recording stopped by itself on download
    finally:
        _close(s)

    route = netbanking.get_route("HDFC")
    actions = [(st["action"], st.get("role") or st.get("label")) for st in route["steps"]]
    assert actions[0] == ("click", "Account Statement") and actions[-1] == ("click", "Download")
    # A date set by a calendar pop-up is reported a moment later, so the middle order can vary.
    assert sorted(actions[1:-1]) == sorted(
        [("select", "selAcct"), ("select", "Statement period"), ("date", "from"), ("date", "to"), ("select", "fmt")]
    )
    by_label = {st.get("field_label"): st for st in route["steps"] if st.get("field_label")}
    assert by_label["Statement period"]["month_format"] == "%B %Y"
    assert {"From date", "To date"} <= set(by_label)
    saved = json.dumps(route)
    assert "#bb_input_" not in saved  # auto-numbered ids are never relied on
    with db.session() as conn:  # raw recording, before get_route's clean-up of older routes
        raw = json.loads(db.get_setting(conn, "netbanking_route:HDFC"))
    assert not [st for st in raw["steps"] if st["candidates"][-1] == "html > body > a"]  # page's scripted click ignored
    for secret in (SECRET_PASSWORD, SECRET_USER, SECRET_OTP, "01/10/2026"):
        assert secret not in saved  # login fields never recorded; dates are re-filled on replay

    # Next visit: you log in, the app does the rest for a new period.
    s = _open(account, f"{bank.base}/login.html")
    try:
        s.call(_login).result(30)
        result = s.command("replay", start=date(2026, 11, 1), end=date(2026, 11, 15)).result(120)
        assert result["ok"], s.snapshot()
        # Field ids changed since recording; the month picker follows the new period.
        assert bank.downloads[-1] == {"acct": "A1", "period": "November 2026", "from": "01/11/2026", "to": "15/11/2026", "fmt": "XLS"}
        imports = _wait_imports(s, 1)
        assert imports[0]["ok"] and imports[0]["n_new"] == 2
        assert len(s.snapshot()["imports"]) == 1 and len(bank.downloads) == 2
    finally:
        _close(s)
    dates = {t["txn_date"] for t in service.list_transactions(month="2026-11")["items"]}
    assert {"2026-11-01", "2026-11-15"} <= dates  # rows from the replayed period


def test_manual_download_without_recording_is_imported(bank, account):
    s = _open(account, f"{bank.base}/login.html")
    try:
        s.call(_login).result(30)

        def download(page):
            page.frame(name="left_menu").click("text=Account Statement")
            main = page.frame(name="main_part")
            main.wait_for_url("**/statement.html")
            main.get_by_label("From date").fill("01/09/2026")
            main.click("#cal a")
            main.click("#btnDownload")

        s.call(download).result(30)
        imports = _wait_imports(s, 1)
        assert imports[0]["ok"] and imports[0]["account_name"] == "HDFC Savings"
    finally:
        _close(s)
    assert netbanking.get_route("HDFC") is None  # nothing recorded unless you ask


def test_replay_reports_missing_step(bank, account):
    netbanking.save_route("HDFC", [{"action": "click", "candidates": ["#does-not-exist"], "label": "Gone", "frame": "", "tag": "a"}])
    monkey_timeout = netbanking.STEP_TIMEOUT
    netbanking.STEP_TIMEOUT = 2
    s = _open(account, f"{bank.base}/login.html")
    try:
        s.call(_login).result(30)
        result = s.command("replay", start=date(2026, 11, 1), end=date(2026, 11, 2)).result(60)
        assert result == {"ok": False, "failed_step": 1}
        assert "Couldn't find step 1" in s.snapshot()["message"]
    finally:
        netbanking.STEP_TIMEOUT = monkey_timeout
        _close(s)


# ── Pure helpers (no browser) ────────────────────────────────────────────────


def test_date_formats():
    assert netbanking.date_format("01/10/2026") == ("%d/%m/%Y", False)
    assert netbanking.date_format("2026-10-01") == ("%Y-%m-%d", False)
    assert netbanking.date_format("01-OCT-2026") == ("%d-%b-%Y", True)
    assert netbanking.date_format("01 Oct 26") == ("%d %b %y", False)
    assert netbanking.date_format("hello") is None
    assert netbanking.format_date(date(2026, 11, 5), "%d-%b-%Y", upper=True) == "05-NOV-2026"


def test_finalize_assigns_period_roles_and_drops_values():
    raw = [
        {"action": "click", "candidates": ["text=Statement"], "label": "Statement", "frame": ""},
        {"action": "date", "candidates": ["#to"], "label": "to", "frame": "", "value": "30/11/2026"},
        {"action": "date", "candidates": ["#from"], "label": "from", "frame": "", "value": "01/11/2026"},
        {"action": "select", "candidates": ["#fmt"], "label": "fmt", "frame": "", "value": "PDF", "option_text": "PDF"},
        {"action": "select", "candidates": ["#fmt"], "label": "fmt", "frame": "", "value": "XLS", "option_text": "Excel"},
        {"action": "click", "candidates": ["#go"], "label": "Go", "frame": ""},
        {"action": "click", "candidates": ["#go"], "label": "Go", "frame": ""},
    ]
    steps = netbanking.finalize_steps(raw)
    assert [s["action"] for s in steps] == ["click", "date", "date", "select", "click"]
    roles = {s["candidates"][0]: s["role"] for s in steps if s["action"] == "date"}
    assert roles == {"#from": "from", "#to": "to"}
    assert all("value" not in s for s in steps if s["action"] == "date")
    assert steps[3]["value"] == "XLS"


def test_month_options():
    assert netbanking.month_option_format("November 2026") == "%B %Y"
    assert netbanking.month_option_format("Nov-26") == "%b-%y"
    assert netbanking.month_option_format("Savings XX1234") is None
    assert netbanking.month_option_format("Excel") is None


def test_older_routes_are_upgraded():
    netbanking.save_route("HDFC", [
        {"action": "select", "candidates": ["#p"], "label": "p", "frame": "", "option_text": "November 2026", "value": "2: Object"},
        {"action": "click", "candidates": ["button:text-is(\"Download\")"], "label": "Download", "frame": ""},
        {"action": "click", "candidates": ["html > body > a"], "label": "a", "frame": ""},
    ])
    steps = netbanking.get_route("HDFC")["steps"]
    assert [s["action"] for s in steps] == ["select", "click"]
    assert steps[0]["month_format"] == "%B %Y"


def test_login_url_must_be_https():
    with pytest.raises(ValueError):
        netbanking.set_login_url("HDFC", "http://evil.example")
    netbanking.set_login_url("HDFC", "https://netbanking.hdfc.bank.in/")
    assert netbanking.login_url("HDFC") == "https://netbanking.hdfc.bank.in/"
