"""Backend logging: useful lines, never personal data."""

import logging

from fastapi.testclient import TestClient
from helpers import MONTH1, hdfc_xlsx

from tracker import logs, service
from tracker.importer import import_file


def _client():
    import app as app_module

    return TestClient(app_module.app, raise_server_exceptions=False), app_module


def test_requests_logged_without_query_string(caplog):
    client, _ = _client()
    with caplog.at_level(logging.INFO):
        client.get("/api/transactions", params={"q": "RAMESH KUMAR"})
    assert "GET /api/transactions -> 200" in caplog.text
    assert "RAMESH" not in caplog.text  # search text never reaches the log


def test_unexpected_error_is_logged_with_traceback(caplog, monkeypatch):
    client, app_module = _client()

    def boom(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(app_module.insights, "overview", boom)
    with caplog.at_level(logging.INFO):
        r = client.get("/api/overview")
    assert r.status_code == 500 and "data/logs/app.log" in r.json()["error"]
    assert "Unhandled error in GET /api/overview" in caplog.text
    assert "kaboom" in caplog.text  # traceback included


def test_import_and_categorize_log_counts_not_content(tmp_path, fake_llm, caplog):
    service.create_account(name="HDFC Savings", bank="HDFC", last4="1234")
    with caplog.at_level(logging.INFO):
        import_file(hdfc_xlsx(tmp_path / "apr.xlsx", MONTH1), classifier=fake_llm)
    assert "Imported statement" in caplog.text and f"{len(MONTH1)} rows" in caplog.text
    assert "Categorized" in caplog.text
    for secret in ("SWIGGY", "RAMESH", "KUMAR STORES", "450", "49550"):
        assert secret not in caplog.text


def test_log_file_written_and_rotating():
    path = logs.setup("app")
    logging.getLogger("tests").info("log-file-check")
    for h in logging.getLogger().handlers:
        h.flush()
    assert "log-file-check" in path.read_text(encoding="utf-8")
    assert path.parent.name == "logs"
