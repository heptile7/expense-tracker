"""The home-screen app pieces: manifest, service worker, offline page and the share target."""

import json

from fastapi.testclient import TestClient

import config

PHONE = ("100.101.102.103", 50000)


def _client():
    import app as app_module

    return TestClient(app_module.app, client=PHONE, follow_redirects=False)


def test_manifest_makes_it_installable_and_a_share_target():
    r = _client().get("/manifest.webmanifest")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/manifest+json")
    m = json.loads(r.text)
    assert m["display"] == "standalone" and m["scope"] == "/" and m["start_url"].startswith("/")
    sizes = {i["sizes"] for i in m["icons"]}
    assert {"192x192", "512x512"} <= sizes and any(i["purpose"] == "maskable" for i in m["icons"])
    for icon in m["icons"]:
        assert (config.BASE_DIR / icon["src"].lstrip("/")).exists(), icon["src"]
    share = m["share_target"]
    assert share["action"] == "/share" and share["method"] == "POST" and share["enctype"] == "multipart/form-data"
    assert "application/pdf" in share["params"]["files"][0]["accept"]


def test_service_worker_covers_the_whole_app_and_caches_no_data():
    r = _client().get("/sw.js")  # before signing in: the phone needs it to install the app
    assert r.status_code == 200 and "javascript" in r.headers["content-type"]
    assert r.headers["cache-control"] == "no-cache" and r.headers["service-worker-allowed"] == "/"
    assert "/share" in r.text and "/api" not in r.text  # API responses are never cached on the phone


def test_offline_page_and_share_without_a_service_worker():
    c = _client()
    assert "Can't reach the app" in c.get("/offline.html").text
    r = c.post("/share", files={"file": ("statement.pdf", b"%PDF-1.4 test", "application/pdf")})
    assert r.status_code == 303 and r.headers["location"] == "/?shared=none#import"  # nothing is stored on the server


def test_page_links_the_manifest_and_has_the_phone_menu():
    html = _client().get("/").text
    assert 'rel="manifest" href="/manifest.webmanifest"' in html
    assert 'id="bottom-nav"' in html and 'id="dlg-pick"' in html and "viewport-fit=cover" in html
