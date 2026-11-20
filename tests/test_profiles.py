"""Sign-in and separate spaces: each person sees only their own data, behind their own PIN."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from helpers import MONTH1, MONTH2, OWNER_PIN, hdfc_xlsx, signed_in_client

from tracker import llm, profiles
from tracker.fetch import netbanking, secrets

PHONE = ("100.101.102.103", 50000)  # a phone on the private network, through the Tailscale relay


@pytest.fixture
def app_module(monkeypatch, fake_llm):
    import app as app_module

    monkeypatch.setattr("tracker.categorizer.default_classifier", lambda *a, **k: fake_llm)
    monkeypatch.setattr(app_module.secrets, "get_pdf_password", lambda _id: None)
    return app_module


def _phone(app_module) -> TestClient:
    return TestClient(app_module.app, client=PHONE)


def _upload(client, path, **form):
    with open(path, "rb") as f:
        return client.post("/api/import", files={"file": (path.name, f)}, data=form)


def _sign_in(client, name, pin):
    people = {p["name"]: p["id"] for p in client.get("/api/auth/state").json()["people"]}
    return client.post("/api/auth/signin", json={"profile_id": people[name], "pin": pin})


def test_nothing_without_signing_in(app_module):
    c = TestClient(app_module.app, client=("127.0.0.1", 50000), base_url="http://127.0.0.1:8765")
    assert c.get("/").status_code == 200  # the page itself shows the sign-in screen
    for path in ("/api/meta", "/api/transactions", "/api/where", "/api/accounts", "/api/openapi.json"):
        r = c.get(path)
        assert r.status_code == 401 and r.json()["signin"], path
    state = c.get("/api/auth/state").json()
    assert state["setup_needed"] and state["can_setup"] and state["me"] is None


def test_setup_only_on_this_computer(app_module):
    assert _phone(app_module).post("/api/auth/setup", json={"name": "Phone", "pin": "1234"}).status_code == 403
    relayed = TestClient(app_module.app, client=("127.0.0.1", 50000), base_url="http://127.0.0.1:8765", headers={"X-Forwarded-For": "100.64.0.9"})
    assert relayed.post("/api/auth/setup", json={"name": "Relay", "pin": "1234"}).status_code == 403
    c = TestClient(app_module.app, client=("127.0.0.1", 50000), base_url="http://127.0.0.1:8765")
    assert c.post("/api/auth/setup", json={"name": "Hema", "pin": "12"}).status_code == 400  # PIN too short
    r = c.post("/api/auth/setup", json={"name": "Hema", "pin": OWNER_PIN})
    assert r.status_code == 200 and r.json()["me"]["is_owner"]
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    assert c.get("/api/meta").status_code == 200
    assert c.post("/api/auth/setup", json={"name": "Again", "pin": "9999"}).status_code == 400


def test_each_person_sees_only_their_own_data(app_module, tmp_path):
    owner = signed_in_client(app_module)
    owner.post("/api/accounts", json={"name": "HDFC Savings", "bank": "HDFC", "last4": "1234"}).raise_for_status()
    assert _upload(owner, hdfc_xlsx(tmp_path / "mine.xlsx", MONTH1)).status_code == 200
    amma = owner.post("/api/people", json={"name": "Amma", "pin": "4321"}).json()
    assert not amma["is_owner"]

    phone = _phone(app_module)
    assert {p["name"] for p in phone.get("/api/auth/state").json()["people"]} == {"Test User", "Amma"}
    assert _sign_in(phone, "Amma", "4321").status_code == 200
    assert phone.get("/api/accounts").json() == [] and phone.get("/api/transactions").json()["total"] == 0
    phone.post("/api/accounts", json={"name": "Amma HDFC", "bank": "HDFC", "last4": "1234"}).raise_for_status()
    assert _upload(phone, hdfc_xlsx(tmp_path / "amma.xlsx", MONTH2)).status_code == 200

    mine = {t["txn_date"][:7] for t in owner.get("/api/transactions").json()["items"]}
    hers = {t["txn_date"][:7] for t in phone.get("/api/transactions").json()["items"]}
    assert mine == {"2024-04"} and hers == {"2024-05"}
    assert [a["name"] for a in owner.get("/api/accounts").json()] == ["HDFC Savings"]
    assert (profiles.base_dir() / "profiles" / "amma" / "expenses.db").exists()
    assert any((profiles.base_dir() / "profiles" / "amma" / "inbox" / "uploads").iterdir())

    # Only the owner adds people; everyone else sees just themselves.
    assert phone.post("/api/people", json={"name": "Appa", "pin": "5555"}).status_code == 403
    assert [p["name"] for p in phone.get("/api/people").json()] == ["Amma"]
    assert phone.patch(f"/api/people/{owner.get('/api/people').json()[0]['id']}", json={"pin": "0000"}).status_code == 403


def test_wrong_pins_lock_you_out(app_module):
    signed_in_client(app_module).post("/api/people", json={"name": "Appa", "pin": "2468"}).raise_for_status()
    phone = _phone(app_module)
    for left in (4, 3, 2, 1):
        r = _sign_in(phone, "Appa", "0000")
        assert r.status_code == 401 and f"{left} tr" in r.json()["error"]
    assert "10 minutes" in _sign_in(phone, "Appa", "0000").json()["error"]
    assert _sign_in(phone, "Appa", "2468").status_code == 401  # even the right PIN, until the lock ends
    assert phone.get("/api/meta").status_code == 401


def test_new_pin_signs_out_other_devices(app_module):
    signed_in_client(app_module).post("/api/people", json={"name": "Amma", "pin": "4321"}).raise_for_status()
    phone, tablet = _phone(app_module), _phone(app_module)
    _sign_in(phone, "Amma", "4321").raise_for_status()
    _sign_in(tablet, "Amma", "4321").raise_for_status()
    me = phone.get("/api/people").json()[0]
    assert phone.patch(f"/api/people/{me['id']}", json={"pin": "7777", "current_pin": "1111"}).status_code == 400
    assert phone.patch(f"/api/people/{me['id']}", json={"pin": "7777", "current_pin": "4321"}).status_code == 200
    assert phone.get("/api/meta").status_code == 200
    assert tablet.get("/api/meta").status_code == 401
    phone.post("/api/auth/signout")
    assert phone.get("/api/meta").status_code == 401


def test_claude_subscription_is_only_for_the_owner(app_module):
    owner = signed_in_client(app_module)
    assert owner.get("/api/settings/categorizer").json()["subscription_allowed"]
    owner.post("/api/people", json={"name": "Amma", "pin": "4321"}).raise_for_status()
    phone = _phone(app_module)
    _sign_in(phone, "Amma", "4321").raise_for_status()
    assert not phone.get("/api/settings/categorizer").json()["subscription_allowed"]
    assert phone.put("/api/settings/categorizer", json={"mode": "claude_code"}).status_code == 400
    amma = next(p for p in profiles.list_profiles() if p.name == "Amma")
    with profiles.use(amma):
        assert llm.build_classifier("claude_code") is None


def test_pdf_passwords_and_net_banking_are_per_person(app_module):
    signed_in_client(app_module).post("/api/people", json={"name": "Amma", "pin": "4321"}).raise_for_status()
    owner, amma = profiles.list_profiles()
    assert owner.is_owner and secrets._key(3) == "pdf-password:3"  # outside a request: the owner, unchanged
    with profiles.use(amma):
        assert secrets._key(3) == "pdf-password:amma:3"
        assert profiles.inbox_dir() == profiles.base_dir() / "profiles" / "amma" / "inbox"
    window = SimpleNamespace(profile_user=amma)
    with profiles.use(amma):
        assert netbanking._mine(window)
    with profiles.use(owner):
        assert not netbanking._mine(window)  # someone else's bank window is invisible to you
