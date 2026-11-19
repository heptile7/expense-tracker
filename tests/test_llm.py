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


def test_no_credits_is_reported(monkeypatch):
    import anthropic
    import httpx

    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx.Response(400, request=req)
    err = anthropic.BadRequestError("Your credit balance is too low to access the Anthropic API.", response=resp, body=None)
    clf, _ = _classifier(monkeypatch, [err])
    assert clf(ITEMS, CATS, []).error == "no API credits left"


# ── Claude Code CLI (subscription) ───────────────────────────────────────────


def _fake_run(monkeypatch, stdout, captured, returncode=0):
    def run(cmd, **kwargs):
        captured.append((cmd, kwargs))
        return SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)

    monkeypatch.setattr(llm.subprocess, "run", run)


def test_claude_code_backend_uses_cli_without_tools_or_api_key(monkeypatch):
    envelope = {
        "type": "result", "subtype": "success", "is_error": False, "stop_reason": "end_turn",
        "result": "", "usage": {"input_tokens": 50, "cache_read_input_tokens": 900, "output_tokens": 80},
        "structured_output": {"results": [{"id": 0, "category": "Groceries", "confidence": 0.85}, {"id": 1, "category": "Rent", "confidence": 0.7}]},
    }
    captured = []
    _fake_run(monkeypatch, json.dumps(envelope), captured)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("CLAUDECODE", "1")
    out = llm.ClaudeCodeClassifier(executable="claude")(ITEMS, CATS, [])
    cmd, kwargs = captured[0]
    assert cmd[:2] == ["claude", "-p"]
    assert cmd[cmd.index("--model") + 1] == "claude-haiku-5-5"
    assert cmd[cmd.index("--tools") + 1] == ""
    assert "--json-schema" in cmd and "--no-session-persistence" in cmd and "--strict-mcp-config" in cmd
    assert "ANTHROPIC_API_KEY" not in kwargs["env"] and "CLAUDECODE" not in kwargs["env"]
    assert "KUMAR STORES" in kwargs["input"]
    assert {i: r.category for i, r in out.results.items()} == {0: "Groceries", 1: "Rent"}
    assert out.input_tokens == 950 and out.call_log[0][0] == "claude-code/claude-haiku-5-5" and out.call_log[0][5] == 0.0


def test_claude_code_usage_limit_is_an_error(monkeypatch):
    _fake_run(monkeypatch, json.dumps({"is_error": True, "subtype": "error_during_execution", "result": "Claude usage limit reached"}), [])
    out = llm.ClaudeCodeClassifier(executable="claude")(ITEMS, CATS, [])
    assert out.results == {} and "usage limit" in out.error


def test_claude_code_missing_cli(monkeypatch):
    monkeypatch.setattr(llm.shutil, "which", lambda _name: None)
    assert "not found" in llm.ClaudeCodeClassifier()(ITEMS, CATS, []).error


def test_chain_falls_through_to_next_backend():
    def failing(items, cats, ex):
        return llm.LLMBatchOutcome(error="no API credits left")

    def working(items, cats, ex):
        out = llm.LLMBatchOutcome(calls=1, backend="sub")
        out.results = {it.id: llm.LLMResult("Groceries", 0.9) for it in items}
        return out

    out = llm.ChainClassifier([failing, working])(ITEMS, CATS, [])
    assert len(out.results) == 2 and out.error is None and out.backend == "sub"
    assert llm.ChainClassifier([failing])(ITEMS, CATS, []).error.endswith("no API credits left")


def test_build_classifier_modes(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(llm.shutil, "which", lambda _name: "claude")
    assert [b.name for b in llm.build_classifier("auto").backends] == ["claude_code"]
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert [b.name for b in llm.build_classifier("auto").backends] == ["api", "claude_code"]
    assert llm.build_classifier("local") is None


def test_upi_ids_are_masked_for_claude():
    assert llm.mask_upi_id("9876543210@ybl") == "#@ybl"
    assert llm.mask_upi_id("swiggy@icici") == "swiggy@icici"


def test_missing_credentials_is_reported_not_raised(monkeypatch):
    clf, fake = _classifier(monkeypatch, [TypeError('"Could not resolve authentication method. Expected one of api_key"')])
    out = clf(ITEMS * 40, CATS, [])  # several batches: only the first is attempted
    assert out.error == llm.NO_CREDENTIALS
    assert len(fake.requests) == 1
