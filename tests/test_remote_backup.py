"""Phone access through Tailscale (status and on/off, only from this computer) and automatic backups."""

import json
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient
from helpers import signed_in_client

from tracker import backup, db, profiles, remote

DNS = "home-pc.tail1234.ts.net"
SERVING = {"TCP": {"443": {"HTTPS": True}}, "Web": {f"{DNS}:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8765"}}}}}


class FakeTailscale:
    """Stands in for the tailscale command line."""

    def __init__(self, signed_in=True, serving=False):
        self.signed_in, self.serving, self.calls = signed_in, serving, []

    def __call__(self, args, timeout=20):
        self.calls.append(args)
        if args == ["status", "--json"]:
            return 0, json.dumps({"BackendState": "Running" if self.signed_in else "NeedsLogin", "Self": {"DNSName": f"{DNS}."}})
        if args == ["serve", "status", "--json"]:
            return 0, json.dumps(SERVING if self.serving else {})
        if args[:2] == ["serve", "--bg"]:
            self.serving = True
            return 0, f"Available within your tailnet:\nhttps://{DNS}/\n|-- proxy http://127.0.0.1:8765"
        if args == ["serve", "--https=443", "off"]:
            self.serving = False
            return 0, ""
        return 1, "unexpected"


@pytest.fixture
def tailscale(monkeypatch):
    fake = FakeTailscale()
    monkeypatch.setattr(remote, "cli", lambda: "tailscale")
    monkeypatch.setattr(remote, "_run", fake)
    return fake


def test_status_reads_the_tailscale_cli(tailscale, monkeypatch):
    assert remote.status() == {"installed": True, "signed_in": True, "serving": False, "address": f"https://{DNS}/", "port": 8765}
    tailscale.serving = True
    assert remote.status()["serving"]
    monkeypatch.setattr(remote, "cli", lambda: None)
    assert remote.status()["installed"] is False


def test_turning_phone_access_on_and_off(tailscale):
    on = remote.turn_on()
    assert on["ok"] and on["serving"] and ["serve", "--bg", "8765"] in tailscale.calls
    off = remote.turn_off()
    assert off["ok"] and not off["serving"] and ["serve", "--https=443", "off"] in tailscale.calls


def test_only_the_owner_on_this_computer_can_switch_it(tailscale):
    import app as app_module

    owner = signed_in_client(app_module)
    assert owner.get("/api/remote").json()["on_this_computer"]
    assert owner.post("/api/remote/on").json()["serving"]
    # The same owner, but through the phone link (the computer's Tailscale name): can look, can't switch.
    cookie = owner.cookies.get(profiles.COOKIE)
    via_link = TestClient(app_module.app, base_url=f"https://{DNS}", client=("127.0.0.1", 50000), cookies={profiles.COOKIE: cookie})
    assert via_link.get("/api/remote").json()["on_this_computer"] is False
    assert via_link.post("/api/remote/off").status_code == 403
    relayed = TestClient(app_module.app, base_url="http://127.0.0.1:8765", client=("127.0.0.1", 50000),
                         headers={"Tailscale-User-Login": "someone@example.com"}, cookies={profiles.COOKIE: cookie})
    assert relayed.post("/api/remote/off").status_code == 403
    owner.post("/api/people", json={"name": "Amma", "pin": "4321"}).raise_for_status()
    amma = TestClient(app_module.app, client=("100.101.102.103", 50000))
    amma_id = next(p["id"] for p in amma.get("/api/auth/state").json()["people"] if p["name"] == "Amma")
    amma.post("/api/auth/signin", json={"profile_id": amma_id, "pin": "4321"}).raise_for_status()
    assert amma.get("/api/remote").status_code == 403 and amma.get("/api/backup").status_code == 403


def test_sign_in_cookie_is_secure_over_the_phone_link():
    import app as app_module

    signed_in_client(app_module)  # sets sign-in up
    phone = TestClient(app_module.app, base_url=f"https://{DNS}", client=("100.101.102.103", 50000))
    owner_id = phone.get("/api/auth/state").json()["people"][0]["id"]
    r = phone.post("/api/auth/signin", json={"profile_id": owner_id, "pin": "123456"})
    assert r.status_code == 200 and "secure" in r.headers["set-cookie"].lower()


def test_backup_folder_must_be_a_real_folder_outside_the_data(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("onedrive")
    with pytest.raises(ValueError):
        backup.set_folder("relative/folder")
    with pytest.raises(ValueError):
        backup.set_folder(str(profiles.base_dir() / "inside"))
    s = backup.set_folder(str(tmp_path / "OneDrive" / "Expenses backups"))
    assert s["folder"].endswith("Expenses backups") and (tmp_path / "OneDrive" / "Expenses backups").is_dir()
    assert backup.set_folder("")["folder"] in ("", None)


def test_backup_copies_everyones_database_and_keeps_the_newest(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("onedrive")
    profiles.create_owner("Owner", "123456")
    amma = profiles.create_profile("Amma", "4321")
    with db.session() as conn:
        conn.execute("INSERT INTO accounts (name, bank) VALUES ('Mine', 'HDFC')")
    with profiles.use(amma), db.session() as conn:
        conn.execute("INSERT INTO accounts (name, bank) VALUES ('Hers', 'CUB')")
    folder = tmp_path / "backups"
    backup.set_folder(str(folder))
    for i in range(10):  # older backups
        (folder / f"{backup.PREFIX}20250101-0000{i:02d}").mkdir()
    assert backup.due()
    r = backup.run()
    assert r["databases"] == 3 and r["removed"] == 11 - backup.KEEP
    newest = folder / r["folder_name"]
    assert sorted(p.name for p in folder.iterdir())[-1] == newest.name and len(list(folder.iterdir())) == backup.KEEP
    names = lambda path: [row[0] for row in sqlite3.connect(path).execute("SELECT name FROM accounts")]  # noqa: E731
    assert names(newest / "expenses.db") == ["Mine"]
    assert names(newest / "profiles" / "amma" / "expenses.db") == ["Hers"]
    assert (newest / "profiles.db").exists() and (newest / "README.txt").exists()
    assert not backup.due()
    assert backup.due(now=time.time() + 8 * 86400)


def test_backup_api(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("onedrive")
    import app as app_module

    owner = signed_in_client(app_module)
    assert owner.get("/api/backup").json()["folder"] is None
    assert owner.post("/api/backup/run").status_code == 400  # no folder yet
    assert owner.put("/api/backup", json={"folder": str(tmp_path / "bk")}).status_code == 200
    r = owner.post("/api/backup/run")
    assert r.status_code == 200 and r.json()["databases"] >= 2 and r.json()["last"]
    phone = TestClient(app_module.app, base_url=f"https://{DNS}", client=("100.101.102.103", 50000),
                       cookies={profiles.COOKIE: owner.cookies.get(profiles.COOKIE)})
    assert phone.put("/api/backup", json={"folder": str(tmp_path / "elsewhere")}).status_code == 403
    assert phone.post("/api/backup/run").status_code == 200  # backing up is fine from anywhere


def test_logs_never_hold_the_backup_folder_or_the_tailscale_address(tailscale, tmp_path_factory, caplog):
    import logging

    folder = tmp_path_factory.mktemp("private-folder-name")
    with caplog.at_level(logging.INFO):
        backup.set_folder(str(folder))
        backup.run()
        remote.turn_on()
        remote.turn_off()
    assert "Backup done" in caplog.text and "Phone access" in caplog.text
    assert "private-folder-name" not in caplog.text and DNS not in caplog.text and "tail1234" not in caplog.text
