"""HaikuClassifier request shape and failure handling, with the Anthropic client faked out."""

import json
from types import SimpleNamespace

import config
from tracker import llm
from tracker.llm import HaikuClassifier, LLMItem

CATS = [{"name": "Groceries", "kind": "expense", "description": "Kirana"}, {"name": "Rent", "kind": "expense", "description": ""}]
ITEMS = [LLMItem(0, "debit", "UPI", "KUMAR STORES", "kumar@oksbi", "UPI-KUMAR STORES-#"), LLMItem(1, "debit", "UPI", "RAMESH", None, "UPI-RAMESH-#")]


class FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _response(payload, stop="end_turn"):
    return SimpleNamespace(
        stop_reason=stop,
        usage=SimpleNamespace(input_tokens=1000, output_tokens=200),
        content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=json.dumps(payload))],
    )


def _classifier(monkeypatch, responses):
    fake = FakeMessages(responses)
    monkeypatch.setattr(llm.anthropic, "Anthropic", lambda: SimpleNamespace(messages=fake))
    return HaikuClassifier(), fake


def test_request_uses_haiku_structured_output_and_no_sampling_params(monkeypatch):
    clf, fake = _classifier(monkeypatch, [_response({"results": [{"id": 0, "category": "Groceries", "confidence": 0.9}]})])
    out = clf(ITEMS, CATS, [("debit | SWIGGY", "Food & Dining")])
    req = fake.requests[0]
    assert req["model"] == config.MODEL_ID == "claude-haiku-5-5"
    assert req["output_config"]["effort"] == "low"
    enum = req["output_config"]["format"]["schema"]["properties"]["results"]["items"]["properties"]["category"]["enum"]
    assert enum == ["Groceries", "Rent", "Uncategorized"]
    assert "temperature" not in req and "top_p" not in req
    user = req["messages"][0]["content"]
    assert "debit | SWIGGY -> Food & Dining" in user and "kumar@oksbi" in user
    assert out.results[0].category == "Groceries" and 1 not in out.results  # thinking block skipped, missing id ignored
    assert out.calls == 1 and abs(out.cost_usd - (1000 * 0.10 + 200 * 0.50) / 1e6) < 1e-12


def test_max_tokens_splits_batch(monkeypatch):
    clf, fake = _classifier(monkeypatch, [
        _response({}, stop="max_tokens"),
        _response({"results": [{"id": 0, "category": "Groceries", "confidence": 0.8}]}),
        _response({"results": [{"id": 1, "category": "Rent", "confidence": 0.7}]}),
    ])
    out = clf(ITEMS, CATS, [])
    assert len(fake.requests) == 3
    assert {i: r.category for i, r in out.results.items()} == {0: "Groceries", 1: "Rent"}


def test_refusal_leaves_items_unanswered(monkeypatch):
    clf, _ = _classifier(monkeypatch, [_response({}, stop="refusal")])
    out = clf(ITEMS, CATS, [])
    assert out.results == {} and out.error is None


def test_missing_credentials_is_reported_not_raised(monkeypatch):
    clf, fake = _classifier(monkeypatch, [TypeError('"Could not resolve authentication method. Expected one of api_key"')])
    out = clf(ITEMS * 40, CATS, [])  # several batches: only the first is attempted
    assert out.error == llm.NO_CREDENTIALS
    assert len(fake.requests) == 1
