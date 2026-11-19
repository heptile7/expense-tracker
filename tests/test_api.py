"""Web API and MCP tools, end to end over the same service layer."""

import pytest
from fastapi.testclient import TestClient
from helpers import MONTH1, MONTH2, hdfc_xlsx


@pytest.fixture
def client(monkeypatch, fake_llm):
    import app as app_module

    monkeypatch.setattr("tracker.categorizer.default_classifier", lambda *a, **k: fake_llm)
    monkeypatch.setattr(app_module.secrets, "get_pdf_password", lambda _id: None)
    return TestClient(app_module.app)


def _upload(client, path, **form):
    with open(path, "rb") as f:
        return client.post("/api/import", files={"file": (path.name, f)}, data=form)


def test_page_and_meta(client):
    assert client.get("/").status_code == 200
    meta = client.get("/api/meta").json()
    assert any(c["name"] == "Food & Dining" for c in meta["categories"])


def test_import_correct_and_learn_over_http(client, tmp_path):
    acct = client.post("/api/accounts", json={"name": "HDFC Savings", "bank": "HDFC", "last4": "1234"}).json()
    april = hdfc_xlsx(tmp_path / "apr.xlsx", MONTH1)
    r = _upload(client, april)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["account_id"] == acct["id"] and body["n_new"] == len(MONTH1)

    assert _upload(client, april).json()["duplicate_file"]
    # A re-export of the same period is a different file, but its rows are already there.
    reexport = _upload(client, hdfc_xlsx(tmp_path / "apr_again.xlsx", MONTH1, account_no="XXXXXXXXXX1234")).json()
    assert reexport["n_new"] == 0 and reexport["n_duplicates"] == len(MONTH1)

    txns = client.get("/api/transactions", params={"q": "RAMESH"}).json()["items"]
    rent = next(c for c in client.get("/api/categories").json() if c["name"] == "Rent")
    res = client.patch(f"/api/transactions/{txns[0]['id']}", json={"category_id": rent["id"], "scope": "merchant"}).json()
    assert res["category"]["category"] == "Rent"

    _upload(client, hdfc_xlsx(tmp_path / "may.xlsx", MONTH2))
    may = client.get("/api/transactions", params={"q": "RAMESH", "month": "2024-05"}).json()["items"]
    assert may[0]["category"] == "Rent" and may[0]["category_source"] == "rule"

    dash = client.get("/api/dashboard", params={"month": "2024-04"}).json()
    labels = {r["label"] for r in dash["by_category"]}
    assert {"Rent", "Food & Dining", "Cash Withdrawal"} <= labels
    assert dash["total_income"] == 85000.0
    assert len(dash["trend"]) == 12


def test_import_needs_account_when_ambiguous(client, tmp_path):
    client.post("/api/accounts", json={"name": "A", "bank": "HDFC", "last4": "1111"})
    client.post("/api/accounts", json={"name": "B", "bank": "HDFC", "last4": "2222"})
    r = _upload(client, hdfc_xlsx(tmp_path / "apr.xlsx", MONTH1))
    assert r.status_code == 409 and r.json()["needs_account"]


def test_bad_file_is_rejected(client, tmp_path):
    bad = tmp_path / "notes.csv"
    bad.write_text("hello,world\n1,2\n")
    client.post("/api/accounts", json={"name": "A", "bank": "HDFC"})
    r = _upload(client, bad)
    assert r.status_code == 422


def test_rules_and_categories_endpoints(client, tmp_path):
    client.post("/api/accounts", json={"name": "HDFC", "bank": "HDFC", "last4": "1234"})
    _upload(client, hdfc_xlsx(tmp_path / "apr.xlsx", MONTH1))
    cat = client.post("/api/categories", json={"name": "Snacks", "kind": "expense"}).json()
    rule = client.post("/api/rules", json={"pattern": "murugan", "category_id": cat["id"], "direction": "debit"}).json()
    assert rule["matched_transactions"] == 1
    assert client.post("/api/rules", json={"pattern": "([", "category_id": cat["id"], "match_type": "regex"}).status_code == 400
    assert client.delete(f"/api/rules/{rule['rule']['id']}").status_code == 200


def test_mcp_tools(tmp_path, fake_llm, monkeypatch):
    import mcp_server
    from tracker import service
    from tracker.importer import import_file

    service.create_account(name="HDFC Savings", bank="HDFC", last4="1234")
    import_file(hdfc_xlsx(tmp_path / "apr.xlsx", MONTH1), classifier=fake_llm)

    summary = mcp_server.spending_summary("2024-04-01", "2024-04-30")
    assert summary["total_income_inr"] == 85000.0
    assert summary["total_spent_inr"] > 0

    found = mcp_server.search_transactions(query="KUMAR STORES")
    assert found["total_matches"] == 1
    out = mcp_server.recategorize(found["transactions"][0]["id"], "household & services")
    assert out["category"] == "Household & Services"

    with pytest.raises(LookupError):
        mcp_server.recategorize(found["transactions"][0]["id"], "Not a category")
    assert mcp_server.review_queue()["total"] >= 1
    assert mcp_server.list_accounts()[0]["transactions"] == len(MONTH1)
