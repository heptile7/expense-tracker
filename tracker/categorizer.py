"""Rule -> cache -> Claude pipeline.

Priority (first match wins):
  1. manual         category set on this one transaction; never touched again
  2. user rules     exact merchant_key, then contains/regex (longest pattern first)
  3. seed rules     shipped keyword list, in config order
  4. llm cache      an earlier Claude answer for this merchant_key
  5. Claude Haiku   one call for all remaining *unique* merchant keys, result cached as a rule
                    (Claude API, or your Claude subscription via the CLI: see llm.py)
  6. local guess    offline naive Bayes trained on your corrections (local_classifier.py), not cached
  7. fallback       Uncategorized, flagged for review
"""

import logging
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field

import config
from tracker import db
from tracker.llm import Classifier, LLMItem, default_classifier, mask_upi_id
from tracker.local_classifier import build_model as build_local_model
from tracker.normalize import mask_for_llm

log = logging.getLogger(__name__)


@dataclass
class Rule:
    id: int
    match_type: str
    pattern: str
    direction: str
    category_id: int
    source: str
    confidence: float | None
    regex: re.Pattern | None = None

    def matches(self, text: str, direction: str) -> bool:
        if self.direction != "any" and self.direction != direction:
            return False
        return bool(self.regex and self.regex.search(text))


def compile_rule(match_type: str, pattern: str, source: str) -> re.Pattern | None:
    """Seeds match whole words (optional plural S); user 'contains' is a plain substring."""
    if match_type == "exact":
        return None
    if match_type == "regex":
        return re.compile(pattern, re.IGNORECASE)
    if source == "seed":
        return re.compile(rf"(?<![A-Z]){re.escape(pattern.upper())}S?(?![A-Z])")
    return re.compile(re.escape(pattern.upper()))


class RuleSet:
    def __init__(self, conn: sqlite3.Connection):
        self.user_exact: dict[str, Rule] = {}
        self.llm_exact: dict[str, Rule] = {}
        self.user_patterns: list[Rule] = []
        self.seeds: list[Rule] = []
        for r in conn.execute("SELECT * FROM merchant_rules ORDER BY id"):
            rule = Rule(r["id"], r["match_type"], r["pattern"], r["direction"], r["category_id"], r["source"], r["confidence"])
            if rule.match_type == "exact":
                (self.user_exact if rule.source == "user" else self.llm_exact)[rule.pattern] = rule
                continue
            try:
                rule.regex = compile_rule(rule.match_type, rule.pattern, rule.source)
            except re.error:
                log.warning("Skipping invalid regex rule %s: %s", rule.id, rule.pattern)
                continue
            (self.seeds if rule.source == "seed" else self.user_patterns).append(rule)
        self.user_patterns.sort(key=lambda r: len(r.pattern), reverse=True)

    def match(self, merchant_key: str, text: str, direction: str) -> tuple[Rule, str] | None:
        """Return (rule, category_source) or None."""
        if merchant_key in self.user_exact:
            return self.user_exact[merchant_key], "rule"
        for rule in self.user_patterns:
            if rule.matches(text, direction):
                return rule, "rule"
        for rule in self.seeds:
            if rule.matches(text, direction):
                return rule, "seed"
        if merchant_key in self.llm_exact:
            return self.llm_exact[merchant_key], "llm"
        return None


def match_text(narration: str) -> str:
    return re.sub(r"\s+", " ", narration.upper()).strip()


@dataclass
class CategorizeStats:
    total: int = 0
    by_source: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    llm_merchants: int = 0
    llm_calls: int = 0
    llm_cost_usd: float = 0.0
    llm_error: str | None = None
    llm_backend: str | None = None
    local_guesses: int = 0

    def as_dict(self) -> dict:
        return {
            "total": self.total,
            "by_source": dict(self.by_source),
            "llm_merchants": self.llm_merchants,
            "llm_calls": self.llm_calls,
            "llm_cost_usd": round(self.llm_cost_usd, 6),
            "llm_error": self.llm_error,
            "llm_backend": self.llm_backend,
            "local_guesses": self.local_guesses,
        }


def _active_categories(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT id, name, kind, description FROM categories WHERE is_active = 1 ORDER BY kind, name")]


def _few_shot_examples(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    rows = conn.execute(
        """SELECT r.pattern, c.name FROM merchant_rules r JOIN categories c ON c.id = r.category_id
           WHERE r.source = 'user' AND r.match_type = 'exact'
           ORDER BY r.updated_at DESC, r.id DESC LIMIT ?""",
        (config.LLM_FEW_SHOT_LIMIT,),
    ).fetchall()
    out = []
    for r in rows:
        prefix, _, name = r["pattern"].partition(":")
        out.append((f"{'debit' if prefix == 'D' else 'credit'} | {name}", r["name"]))
    return out


def _apply(conn, txn_id: int, category_id: int, source: str, rule_id: int | None, confidence: float | None, needs_review: bool):
    conn.execute(
        """UPDATE transactions SET category_id = ?, category_source = ?, rule_id = ?, confidence = ?, needs_review = ?
           WHERE id = ?""",
        (category_id, source, rule_id, confidence, int(needs_review), txn_id),
    )


def categorize(
    conn: sqlite3.Connection,
    txn_ids: list[int] | None = None,
    classifier: Classifier | None = None,
    use_llm: bool = True,
    use_local: bool = True,
) -> CategorizeStats:
    """(Re)categorize the given transactions, or all non-manual ones when txn_ids is None.

    classifier: Claude backend to use; None picks the one chosen in Settings (or none).
    use_local: guess whatever is still open with the offline categorizer.
    """
    stats = CategorizeStats()
    sql = "SELECT id, narration_raw, merchant_key, direction, channel, counterparty, vpa, category_source FROM transactions WHERE category_source != 'manual'"
    params: list = []
    if txn_ids is not None:
        if not txn_ids:
            return stats
        sql += f" AND id IN ({','.join('?' * len(txn_ids))})"
        params = list(txn_ids)
    rows = conn.execute(sql, params).fetchall()
    stats.total = len(rows)

    rules = RuleSet(conn)
    hits: dict[int, int] = defaultdict(int)
    pending: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        found = rules.match(row["merchant_key"], match_text(row["narration_raw"]), row["direction"])
        if row["category_source"] == "transfer" and (found is None or found[1] != "rule"):
            stats.by_source["transfer"] += 1  # automatic transfer detection outranks everything but your rules
            continue
        if found:
            rule, source = found
            low = source == "llm" and (rule.confidence or 0) < config.LOW_CONFIDENCE
            _apply(conn, row["id"], rule.category_id, source, rule.id, rule.confidence if source == "llm" else 1.0, low)
            hits[rule.id] += 1
            stats.by_source[source] += 1
        else:
            pending[row["merchant_key"]].append(row)

    if pending and use_llm:
        clf = classifier if classifier is not None else default_classifier(conn)
        if clf is not None:
            _categorize_with_llm(conn, pending, clf, stats, hits)
    if pending and use_local:
        _categorize_locally(conn, pending, stats)

    unc = db.uncategorized_id(conn)
    for group in pending.values():
        for row in group:
            _apply(conn, row["id"], unc, "fallback", None, None, True)
            stats.by_source["fallback"] += 1

    conn.executemany(
        "UPDATE merchant_rules SET hit_count = hit_count + ? WHERE id = ?", [(n, rid) for rid, n in hits.items()]
    )
    return stats


def _categorize_with_llm(conn, pending: dict[str, list], classifier: Classifier, stats: CategorizeStats, hits) -> None:
    keys = list(pending)
    items = []
    for i, key in enumerate(keys):
        sample = pending[key][0]
        items.append(
            LLMItem(
                id=i,
                direction=sample["direction"],
                channel=sample["channel"] or "OTHER",
                payee=key.partition(":")[2],
                upi_id=mask_upi_id(sample["vpa"]),
                narration=mask_for_llm(sample["narration_raw"]),
            )
        )
    categories = _active_categories(conn)
    by_name = {c["name"]: c["id"] for c in categories}
    outcome = classifier(items, categories, _few_shot_examples(conn))

    stats.llm_merchants = len(items)
    stats.llm_calls = outcome.calls
    stats.llm_cost_usd = outcome.cost_usd
    stats.llm_error = outcome.error
    stats.llm_backend = outcome.backend
    for model, n_items, in_tok, out_tok, stop, cost in outcome.call_log:
        conn.execute(
            "INSERT INTO llm_calls (model, n_items, input_tokens, output_tokens, cost_usd, stop_reason) VALUES (?, ?, ?, ?, ?, ?)",
            (model, n_items, in_tok, out_tok, cost, stop),
        )
    if outcome.error and not outcome.calls:
        conn.execute(
            "INSERT INTO llm_calls (model, n_items, error) VALUES (?, ?, ?)", (config.MODEL_ID, len(items), outcome.error)
        )

    for i, key in enumerate(keys):
        result = outcome.results.get(i)
        if not result or result.category == config.UNCATEGORIZED or result.category not in by_name:
            continue  # stays pending -> fallback; retried on the next run
        category_id = by_name[result.category]
        rule_id = _upsert_llm_rule(conn, key, category_id, result.confidence)
        low = result.confidence < config.LOW_CONFIDENCE
        for row in pending.pop(key):
            _apply(conn, row["id"], category_id, "llm", rule_id, result.confidence, low)
            stats.by_source["llm"] += 1
            if rule_id:
                hits[rule_id] += 1


def _categorize_locally(conn, pending: dict[str, list], stats: CategorizeStats) -> None:
    """Offline guesses for payees no rule or Claude answered. Not cached: Claude can improve them later."""
    model = build_local_model(conn)
    by_name = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM categories WHERE is_active = 1")}
    for key in list(pending):
        sample = pending[key][0]
        guess = model.guess(key.partition(":")[2], sample["narration_raw"], sample["vpa"], sample["direction"], sample["channel"])
        if not guess or guess.category not in by_name:
            continue
        low = guess.confidence < config.LOW_CONFIDENCE
        for row in pending.pop(key):
            _apply(conn, row["id"], by_name[guess.category], "local", None, guess.confidence, low)
            stats.by_source["local"] += 1
            stats.local_guesses += 1


def _upsert_llm_rule(conn, merchant_key: str, category_id: int, confidence: float) -> int | None:
    """Cache Claude's answer for this merchant. Never overwrites a user rule."""
    existing = conn.execute(
        "SELECT id, source FROM merchant_rules WHERE match_type = 'exact' AND pattern = ? AND direction = 'any'",
        (merchant_key,),
    ).fetchone()
    if existing:
        if existing["source"] != "llm":
            return existing["id"]
        conn.execute(
            "UPDATE merchant_rules SET category_id = ?, confidence = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (category_id, confidence, existing["id"]),
        )
        return existing["id"]
    cur = conn.execute(
        "INSERT INTO merchant_rules (match_type, pattern, direction, category_id, source, confidence) "
        "VALUES ('exact', ?, 'any', ?, 'llm', ?)",
        (merchant_key, category_id, confidence),
    )
    return cur.lastrowid
