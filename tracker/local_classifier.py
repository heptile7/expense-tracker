"""Offline categorizer, used when Claude isn't available (or for whatever Claude leaves open).

A small naive Bayes model over the words in the payee name, UPI ID and UPI remark.
It learns from:
  * your corrections and rules (strongest evidence, so "MURUGAN TRADERS" follows
    what you decided for "SRI MURUGAN TRADERS"),
  * Claude's confident earlier answers, when there are any,
  * a built-in list of business-type words (config.LOCAL_LEXICON: MESS -> Food,
    TAILORS -> Personal Care, MEDICALS -> Health, ...).
Payees that look like a person (UPI ID is a phone number, or a name with an initial)
default to "Payments to People" / "Other Income".

Guesses are never cached as rules, so Claude can still improve them later.
"""

import math
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass

import config
from tracker.normalize import STOP_WORDS

MAX_CONFIDENCE = 0.75  # a local guess is never as sure as your own rule
WEIGHT_USER = 4.0
WEIGHT_MANUAL = 2.0
WEIGHT_USER_PATTERN = 2.0
WEIGHT_LLM = 1.0
WEIGHT_LEXICON = 1.0
ALPHA = 0.05  # small smoothing: one clear word ("RENT", "MESS") should be able to decide

_PERSON_CHANNELS = {"UPI", "IMPS", "NEFT", "RTGS", "TRANSFER"}
_BUSINESS_WORDS = {
    "ENTERPRISES", "ENTERPRISE", "AGENCIES", "AGENCY", "CORPORATION", "CORP", "COMPANY", "INDUSTRIES", "INDIA",
    "SERVICES", "SERVICE", "CENTRE", "CENTER", "SHOP", "STALL", "WORLD", "POINT", "PALACE", "MALL", "LTD", "PVT",
    "LLP", "GROUP", "ASSOCIATES", "SONS", "BROTHERS", "BROS", "TRADING", "EXPORTS", "IMPEX", "PAY", "BANK",
}
_LEXICON_WORDS = {w for _, _, words in config.LOCAL_LEXICON for phrase in words for w in phrase.split()}


@dataclass(frozen=True)
class LocalGuess:
    category: str
    confidence: float
    reason: str


def tokenize(payee: str, narration: str = "", vpa: str | None = None, direction: str = "debit") -> set[str]:
    text = f"{payee} {narration}".upper()
    text = re.sub(r"@[A-Z0-9.]+", " ", text)  # UPI handles (@oksbi, @ybl) say nothing about the purchase
    words = {w for w in re.findall(r"[A-Z]{2,}", text) if w not in STOP_WORDS}
    if vpa:
        letters = re.sub(r"[^a-z]", "", vpa.split("@")[0].lower())
        if len(letters) >= 4:
            words.add(f"VPA:{letters.upper()}")  # recognises the same payee even when the name is spelled differently
    words.add(f"DIR:{direction}")
    return words


def looks_like_person(payee: str, vpa: str | None, channel: str | None) -> bool:
    if (channel or "") not in _PERSON_CHANNELS:
        return False
    words = payee.upper().split()
    if not 1 <= len(words) <= 4 or any(not w.isalpha() for w in words):
        return False
    if any(w in _BUSINESS_WORDS or w in _LEXICON_WORDS for w in words):
        return False
    local = (vpa or "").split("@")[0]
    if re.fullmatch(r"(?:91)?\d{10}", local):
        return True  # phone-number UPI ID: almost always a person
    if any(len(w) == 1 for w in words):
        return True  # "VIGNESH M"
    letters = re.sub(r"[^a-z]", "", local.lower())
    return bool(letters) and words[0].lower()[:5] in letters  # rameshkumar@okaxis for RAMESH KUMAR


class LocalModel:
    def __init__(self, category_names: list[str]):
        self.categories = [c for c in category_names if c != config.UNCATEGORIZED]
        self.counts: dict[str, dict[str, float]] = {c: defaultdict(float) for c in self.categories}
        self.totals: dict[str, float] = defaultdict(float)
        self.vocab: set[str] = set()

    def add(self, category: str, tokens: set[str], weight: float) -> None:
        if category not in self.counts:
            return
        for t in tokens:
            self.counts[category][t] += weight
            self.totals[category] += weight
            self.vocab.add(t)

    def predict(self, tokens: set[str]) -> tuple[str, float] | None:
        known = [t for t in tokens if t in self.vocab]
        if not any(not t.startswith("DIR:") for t in known):
            return None  # no word we've seen before: don't pretend
        v = len(self.vocab)
        scores = {}
        for c in self.categories:
            if not self.totals[c]:
                continue
            denom = self.totals[c] + ALPHA * v
            scores[c] = sum(math.log((self.counts[c].get(t, 0.0) + ALPHA) / denom) for t in known)
        if not scores:
            return None
        top = max(scores.values())
        z = sum(math.exp(s - top) for s in scores.values())
        best = max(scores, key=scores.get)
        return best, 1.0 / z

    def guess(self, payee: str, narration: str, vpa: str | None, direction: str, channel: str | None) -> LocalGuess | None:
        pred = self.predict(tokenize(payee, narration, vpa, direction))
        person = looks_like_person(payee, vpa, channel)
        person_cat = "Payments to People" if direction == "debit" else "Other Income"
        if pred:
            cat, p = pred
            if person and p < 0.5 and person_cat in self.counts:
                return LocalGuess(person_cat, 0.5, "looks like a person")
            return LocalGuess(cat, round(min(MAX_CONFIDENCE, p), 3), "similar words")
        if person and person_cat in self.counts:
            return LocalGuess(person_cat, 0.5, "looks like a person")
        return None


def build_model(conn: sqlite3.Connection) -> LocalModel:
    names = [r["name"] for r in conn.execute("SELECT name FROM categories WHERE is_active = 1")]
    model = LocalModel(names)

    for category, direction, phrases in config.LOCAL_LEXICON:
        for d in ("debit", "credit") if direction == "any" else (direction,):
            for phrase in phrases:
                model.add(category, set(phrase.split()) | {f"DIR:{d}"}, WEIGHT_LEXICON)

    # Payees you (or Claude, confidently) categorized, using one of their transactions for the words.
    rows = conn.execute(
        """SELECT r.pattern, r.source, c.name AS category, t.narration_raw, t.vpa
           FROM merchant_rules r JOIN categories c ON c.id = r.category_id
           LEFT JOIN transactions t ON t.id = (SELECT id FROM transactions WHERE merchant_key = r.pattern LIMIT 1)
           WHERE r.match_type = 'exact' AND (r.source = 'user' OR (r.source = 'llm' AND r.confidence >= 0.8))"""
    ).fetchall()
    for r in rows:
        prefix, _, payee = r["pattern"].partition(":")
        direction = "debit" if prefix == "D" else "credit"
        tokens = tokenize(payee, r["narration_raw"] or "", r["vpa"], direction)
        model.add(r["category"], tokens, WEIGHT_USER if r["source"] == "user" else WEIGHT_LLM)

    for r in conn.execute(
        """SELECT t.narration_raw, t.vpa, t.direction, t.merchant_key, c.name AS category
           FROM transactions t JOIN categories c ON c.id = t.category_id WHERE t.category_source = 'manual'"""
    ):
        model.add(r["category"], tokenize(r["merchant_key"].partition(":")[2], r["narration_raw"], r["vpa"], r["direction"]), WEIGHT_MANUAL)

    for r in conn.execute(
        """SELECT r.pattern, r.direction, c.name AS category FROM merchant_rules r JOIN categories c ON c.id = r.category_id
           WHERE r.source = 'user' AND r.match_type = 'contains'"""
    ):
        for d in ("debit", "credit") if r["direction"] == "any" else (r["direction"],):
            model.add(r["category"], tokenize(r["pattern"], direction=d), WEIGHT_USER_PATTERN)
    return model
