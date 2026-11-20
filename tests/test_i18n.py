"""English / Tamil: the dictionary is complete for what people see, and each person gets their own language."""

import json
import re
from collections import Counter
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient
from helpers import signed_in_client

import config
from tracker import i18n, insights, profiles

TA = {k: v for k, v in json.loads(i18n.TA_FILE.read_text(encoding="utf-8")).items() if not k.startswith("_")}
TAMIL = re.compile(r"[஀-௿]")


def test_dictionary_is_well_formed():
    pairs = json.loads(i18n.TA_FILE.read_text(encoding="utf-8"), object_pairs_hook=lambda p: p)
    assert not [k for k, n in Counter(k for k, _ in pairs).items() if n > 1], "duplicate keys"
    for en, ta in TA.items():
        assert set(re.findall(r"\{(\w+)\}", en)) == set(re.findall(r"\{(\w+)\}", ta)), en
        assert ta.strip(), en


def test_every_built_in_category_has_a_tamil_name():
    names = [c[0] for c in config.DEFAULT_CATEGORIES] + [s[0] for subs in config.SUBCATEGORIES.values() for s in subs]
    assert [n for n in names if n not in TA] == []
    assert all(TAMIL.search(TA[n]) or n in ("DTH & Cable",) for n in names)


def test_every_sentence_the_page_builds_is_translated():
    js = (config.BASE_DIR / "static" / "app.js").read_text(encoding="utf-8")
    keys = set(re.findall(r'\bt\("((?:[^"\\]|\\.)+)"', js))
    keys |= set(re.findall(r'\bt\([^()]*?\?\s*"([^"]+)"\s*:\s*"([^"]+)"', js) and
                [x for pair in re.findall(r'\bt\([^()]*?\?\s*"([^"]+)"\s*:\s*"([^"]+)"', js) for x in pair])
    words = set(re.findall(r'plural\([^,]+, "([^"]+)"\)', js))
    keys |= words | {f"{w}s" for w in words}
    assert sorted(k for k in keys if k not in TA) == []


class _Texts(HTMLParser):
    """Visible text and placeholders inside the parts of the page a phone shows."""

    SECTIONS = {"gate", "bottom-nav", "dlg-more", "dlg-pick", "dlg-category", "tab-dashboard", "tab-review", "people-card"}
    VOID = {"input", "img", "br", "meta", "link", "source"}

    def __init__(self):
        super().__init__()
        self.stack: list[bool] = []
        self.found: set[str] = set()

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        inside = (self.stack and self.stack[-1]) or a.get("id") in self.SECTIONS
        if inside and "data-noi18n" not in a:
            for k in ("placeholder", "aria-label"):
                if a.get(k):
                    self.found.add(a[k])
        if tag not in self.VOID:
            self.stack.append(bool(inside) and "data-noi18n" not in a)

    def handle_endtag(self, tag):
        if tag not in self.VOID and self.stack:
            self.stack.pop()

    def handle_data(self, data):
        text = re.sub(r"\s+", " ", data).strip()
        if self.stack and self.stack[-1] and text and not text.startswith("{") and re.search(r"[A-Za-z]{2}", text):
            self.found.add(text)


def test_phone_screens_are_fully_translated():
    parser = _Texts()
    parser.feed((config.BASE_DIR / "templates" / "index.html").read_text(encoding="utf-8"))
    brand = {"Expenses", "PIN"}
    assert sorted(t for t in parser.found - brand if t not in TA) == []


@pytest.fixture
def tamil_person():
    import app as app_module

    owner = signed_in_client(app_module)
    owner.post("/api/people", json={"name": "Amma", "pin": "4321"}).raise_for_status()
    phone = TestClient(app_module.app, client=("100.101.102.103", 50000))
    amma = next(p for p in owner.get("/api/auth/state").json()["people"] if p["name"] == "Amma")
    phone.post("/api/auth/signin", json={"profile_id": amma["id"], "pin": "4321"}).raise_for_status()
    r = phone.patch(f"/api/people/{amma['id']}", json={"language": "ta"})
    assert r.status_code == 200 and r.json()["language"] == "ta"
    assert phone.patch(f"/api/people/{amma['id']}", json={"language": "fr"}).status_code == 400
    return owner, phone


def test_each_person_has_their_own_language(tamil_person):
    owner, phone = tamil_person
    assert phone.get("/api/auth/state").json()["me"]["language"] == "ta"
    assert owner.get("/api/auth/state").json()["me"]["language"] == "en"


def test_server_sentences_follow_the_language(tamil_person):
    amma = next(p for p in profiles.list_profiles() if p.name == "Amma")
    with profiles.use(amma):
        assert i18n.lang() == "ta"
        assert insights._month_name("2026-10") == "அக்டோபர் 2026"
        assert i18n.say("You spent {amount} more than came in during {period}.", amount="₹500", period="அக்டோபர் 2026") \
            == "அக்டோபர் 2026-இல் வந்ததை விட ₹500 அதிகமாகச் செலவானது."
        assert i18n.word("Rent") == "வாடகை" and i18n.word("Some new category") == "Some new category"
        assert i18n.say("A sentence nobody translated yet {x}", x=1) == "A sentence nobody translated yet 1"
    assert insights._month_name("2026-10") == "October 2026"  # outside a request: English


def test_every_server_sentence_is_translated(monkeypatch, tamil_person):
    """Run the dashboard and "where the money went" as a Tamil speaker and collect every sentence asked for."""
    asked: list[str] = []
    real_say, real_word = i18n.say, i18n.word

    def record_say(en, **p):
        asked.append(en)
        return real_say(en, **p)

    def record_word(en):
        asked.append(en)
        return real_word(en)

    monkeypatch.setattr("tracker.insights.say", record_say)
    monkeypatch.setattr("tracker.insights.word", record_word)
    monkeypatch.setattr("tracker.flow.say", record_say)
    # Every sentence template in the code, whether or not today's data triggers it.
    for module in ("insights", "flow"):
        src = (config.BASE_DIR / "tracker" / f"{module}.py").read_text(encoding="utf-8")
        for call in re.findall(r'\bsay\(((?:"(?:[^"\\]|\\.)*"\s*)+)', src):
            asked.append("".join(re.findall(r'"((?:[^"\\]|\\.)*)"', call)))
        for call in re.findall(r'\badd\("\w+",\s*((?:"(?:[^"\\]|\\.)*"\s*)+)', src):
            asked.append("".join(re.findall(r'"((?:[^"\\]|\\.)*)"', call)))
    _, phone = tamil_person
    phone.get("/api/where", params={"period": "12m"}).raise_for_status()
    phone.get("/api/overview").raise_for_status()
    missing = sorted({k.replace("\\'", "'") for k in asked} - set(TA) - {"Some new category"})
    assert missing == []
