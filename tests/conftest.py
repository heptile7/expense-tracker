import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# Keep everything (DB, uploads) out of the real data/ folder, even modules that init the DB on import.
os.environ["EXPENSE_TRACKER_DATA"] = tempfile.mkdtemp(prefix="expense-tracker-test-")

import pytest

from tracker import db
from tracker.llm import LLMBatchOutcome, LLMResult


class FakeClassifier:
    """Stands in for Claude Haiku: maps payee substrings to categories and records every call."""

    def __init__(self, mapping: dict[str, tuple[str, float]] | None = None, error: str | None = None):
        self.mapping = mapping or {}
        self.error = error
        self.calls: list[dict] = []

    def __call__(self, items, categories, examples):
        self.calls.append({"items": items, "categories": categories, "examples": examples})
        out = LLMBatchOutcome(calls=1, input_tokens=100 * len(items), output_tokens=20 * len(items))
        out.call_log.append(("fake-model", len(items), out.input_tokens, out.output_tokens, "end_turn", 0.0))
        if self.error:
            return LLMBatchOutcome(error=self.error)
        for it in items:
            for needle, (cat, conf) in self.mapping.items():
                if needle in it.payee or (it.upi_id and needle.lower() in it.upi_id):
                    out.results[it.id] = LLMResult(cat, conf)
                    break
        return out

    @property
    def payees_seen(self) -> list[str]:
        return [it.payee for call in self.calls for it in call["items"]]


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    db.set_db_path(path)
    db.init_db()
    # Never hit the real Claude API from unit tests.
    disabled = FakeClassifier(error="LLM disabled in tests")
    monkeypatch.setattr("tracker.categorizer.default_classifier", lambda *a, **k: disabled)
    yield path


@pytest.fixture
def fake_llm():
    return FakeClassifier(
        {
            "KUMAR STORES": ("Groceries", 0.75),
            "SRI MURUGAN TRADERS": ("Shopping", 0.4),
            "RAMESH KUMAR": ("Payments to People", 0.8),
            "GREENLEAF": ("Food & Dining", 0.9),
        }
    )
