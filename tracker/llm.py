"""Claude categorizers: one request per batch of unique, unknown payees.

Two ways to reach Claude Haiku 5.5, tried in order by ChainClassifier:
  * HaikuClassifier      - Anthropic API with ANTHROPIC_API_KEY (billed per token).
  * ClaudeCodeClassifier - the official `claude` CLI in print mode, signed in with your own
                           Claude subscription (counts toward your plan's usage limits).
Whatever Claude can't answer falls through to the offline categorizer (local_classifier.py).

Only masked narration text, the payee name, the UPI ID (digits masked), channel and direction
are sent. Amounts, balances, account numbers and reference numbers never leave the machine.
"""

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import anthropic

import config

log = logging.getLogger(__name__)


@dataclass
class LLMItem:
    id: int
    direction: str
    channel: str
    payee: str
    upi_id: str | None  # digits already masked
    narration: str  # already masked


@dataclass
class LLMResult:
    category: str
    confidence: float


@dataclass
class LLMBatchOutcome:
    results: dict[int, LLMResult] = field(default_factory=dict)
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    calls: int = 0
    error: str | None = None
    backend: str | None = None  # label of the backend that answered
    # (model, n_items, input_tokens, output_tokens, stop_reason, cost_usd) per call, written to llm_calls by the caller
    call_log: list[tuple] = field(default_factory=list)


# (items, categories, examples) -> outcome. Tests inject a fake with this signature.
Classifier = Callable[[list[LLMItem], list[dict], list[tuple[str, str]]], LLMBatchOutcome]

NO_CREDENTIALS = "No ANTHROPIC_API_KEY set"

SYSTEM_PROMPT = """You categorize transactions from Indian bank statements for a personal expense tracker.

Each item has the payee/payer name, UPI ID if any, payment channel, direction (debit = money out,
credit = money in) and the narration with numbers masked as '#'. Use them to work out what kind of
merchant or person this is, then choose exactly one category from the list for every item.

Guidance:
- Pick the most specific sub-category that fits (e.g. "Food Delivery" rather than "Food & Dining"); use the
  main category only when none of its sub-categories fit.
- Well-known Indian merchants, apps and billers: use what you know about them (UPI IDs often name the merchant).
- A debit to what looks like an individual's name with no other clue: "Payments to People".
- A credit from an individual with no other clue: "Other Income".
- Small shops (stores, traders, enterprises) with no clear type: pick the most likely category with lower confidence.
- If you genuinely cannot tell, answer "Uncategorized".
- The user's own past decisions are listed as examples; follow their preferences for similar payees.

confidence: 0.9-1.0 when the merchant is well known or the name is unambiguous, 0.6-0.8 for a reasonable
inference, below 0.6 when guessing."""


def mask_upi_id(vpa: str | None) -> str | None:
    """9876543210@ybl -> #@ybl. Phone numbers in UPI IDs belong to other people."""
    return re.sub(r"\d{4,}", "#", vpa) if vpa else None


def _schema(category_names: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "category": {"type": "string", "enum": category_names},
                        "confidence": {"type": "number"},
                    },
                    "required": ["id", "category", "confidence"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["results"],
        "additionalProperties": False,
    }


def _category_names(categories: list[dict]) -> list[str]:
    names = [c["name"] for c in categories]
    if config.UNCATEGORIZED not in names:
        names.append(config.UNCATEGORIZED)
    return names


def _user_message(items: list[LLMItem], categories: list[dict], examples: list[tuple[str, str]]) -> str:
    cat_lines = "\n".join(f"- {c['name']} ({c['kind']}): {c.get('description') or ''}".rstrip(": ") for c in categories)
    parts = [f"Categories:\n{cat_lines}"]
    if examples:
        ex_lines = "\n".join(f"- {payee} -> {cat}" for payee, cat in examples)
        parts.append(f"The user's own categorizations (follow these preferences):\n{ex_lines}")
    payload = [
        {"id": it.id, "direction": it.direction, "channel": it.channel, "payee": it.payee, "upi_id": it.upi_id, "narration": it.narration}
        for it in items
    ]
    parts.append("Transactions to categorize:\n" + json.dumps(payload, ensure_ascii=False, indent=1))
    return "\n\n".join(parts)


def _collect(data: dict, items: list[LLMItem], names: list[str], outcome: LLMBatchOutcome) -> None:
    valid_ids = {it.id for it in items}
    for r in data.get("results", []) if isinstance(data, dict) else []:
        if r.get("id") in valid_ids and r.get("category") in names:
            conf = max(0.0, min(1.0, float(r.get("confidence", 0.5))))
            outcome.results[r["id"]] = LLMResult(r["category"], conf)


class HaikuClassifier:
    """Anthropic API (needs ANTHROPIC_API_KEY with credits)."""

    name = "api"
    label = "Claude API"

    def __init__(self, model: str = config.MODEL_ID):
        self.model = model
        self._client: anthropic.Anthropic | None = None
        self._init_error: str | None = None

    def _get_client(self) -> anthropic.Anthropic | None:
        if self._client is None and self._init_error is None:
            try:
                self._client = anthropic.Anthropic()
            except Exception as exc:  # missing credentials, bad config
                self._init_error = f"Claude API unavailable: {exc}"
        return self._client

    def __call__(self, items: list[LLMItem], categories: list[dict], examples: list[tuple[str, str]]) -> LLMBatchOutcome:
        outcome = LLMBatchOutcome(backend=self.label)
        client = self._get_client()
        if client is None:
            outcome.error = self._init_error
            return outcome
        names = _category_names(categories)
        for start in range(0, len(items), config.LLM_BATCH_SIZE):
            if self._init_error:
                outcome.error = self._init_error
                break
            self._classify(client, items[start : start + config.LLM_BATCH_SIZE], categories, names, examples, outcome)
            if outcome.error:
                break  # don't hammer a failing API with the remaining batches
        return outcome

    def _classify(self, client, items, categories, names, examples, outcome: LLMBatchOutcome) -> None:
        try:
            response = client.messages.create(
                model=self.model,
                max_tokens=config.LLM_MAX_TOKENS,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": _user_message(items, categories, examples)}],
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": _schema(names)}},
            )
        except anthropic.RateLimitError as exc:
            outcome.error = f"rate limited, try again shortly ({exc.status_code})"
            return
        except anthropic.AuthenticationError:
            outcome.error = "API key is invalid (check ANTHROPIC_API_KEY in .env)"
            return
        except anthropic.APIStatusError as exc:
            msg = str(exc.message)
            outcome.error = "no API credits left" if "credit balance" in msg.lower() else f"error {exc.status_code}: {msg}"
            return
        except anthropic.APIConnectionError:
            outcome.error = "could not reach the API (network error)"
            return
        except TypeError as exc:  # SDK raises this when no API key / auth is configured
            if "authentication" not in str(exc).lower():
                raise
            self._init_error = outcome.error = NO_CREDENTIALS
            self._client = None
            return
        except anthropic.AnthropicError as exc:
            outcome.error = f"error: {exc}"
            return

        usage = response.usage
        cost = (usage.input_tokens * config.PRICE_INPUT_PER_MTOK + usage.output_tokens * config.PRICE_OUTPUT_PER_MTOK) / 1_000_000
        outcome.calls += 1
        outcome.input_tokens += usage.input_tokens
        outcome.output_tokens += usage.output_tokens
        outcome.cost_usd += cost
        outcome.call_log.append((self.model, len(items), usage.input_tokens, usage.output_tokens, response.stop_reason, cost))

        if response.stop_reason == "refusal":
            log.warning("Claude declined to categorize a batch of %d items", len(items))
            return
        if response.stop_reason == "max_tokens":
            if len(items) > 1:
                mid = len(items) // 2
                self._classify(client, items[:mid], categories, names, examples, outcome)
                self._classify(client, items[mid:], categories, names, examples, outcome)
            return
        text = next((b.text for b in response.content if b.type == "text"), None)
        try:
            _collect(json.loads(text or "{}"), items, names, outcome)
        except json.JSONDecodeError:
            log.warning("Unparseable categorizer output: %.200s", text)


class ClaudeCodeClassifier:
    """Runs the official `claude` CLI in print mode, signed in with your own Claude subscription.

    Each call uses no tools, no MCP servers and no settings files, and is not saved as a session.
    Usage counts toward your plan's limits; there is no per-token bill.
    """

    name = "claude_code"
    label = "Claude (your subscription)"
    TIMEOUT = 180

    def __init__(self, model: str = config.MODEL_ID, executable: str | None = None):
        self.model = model
        self.executable = executable or shutil.which("claude")

    def __call__(self, items: list[LLMItem], categories: list[dict], examples: list[tuple[str, str]]) -> LLMBatchOutcome:
        outcome = LLMBatchOutcome(backend=self.label)
        if not self.executable:
            outcome.error = "Claude Code CLI not found (install Claude Code and sign in with `claude`)"
            return outcome
        names = _category_names(categories)
        for start in range(0, len(items), config.LLM_BATCH_SIZE):
            self._classify(items[start : start + config.LLM_BATCH_SIZE], categories, names, examples, outcome)
            if outcome.error:
                break
        return outcome

    def command(self, names: list[str]) -> list[str]:
        return [
            self.executable, "-p",
            "--model", self.model,
            "--output-format", "json",
            "--json-schema", json.dumps(_schema(names)),
            "--system-prompt", SYSTEM_PROMPT,
            "--tools", "",
            "--strict-mcp-config",
            "--setting-sources", "",
            "--no-session-persistence",
        ]

    @staticmethod
    def environment() -> dict[str, str]:
        # Drop API keys so the CLI uses your subscription sign-in, and the marker that says
        # we're already inside a Claude Code session.
        skip = {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"}
        return {k: v for k, v in os.environ.items() if k not in skip}

    def _classify(self, items, categories, names, examples, outcome: LLMBatchOutcome) -> None:
        started = time.monotonic()
        try:
            proc = subprocess.run(
                self.command(names),
                input=_user_message(items, categories, examples),
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=self.TIMEOUT,
                cwd=tempfile.gettempdir(),
                env=self.environment(),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0,
            )
        except FileNotFoundError:
            outcome.error = "Claude Code CLI not found"
            return
        except subprocess.TimeoutExpired:
            outcome.error = f"Claude Code didn't answer within {self.TIMEOUT}s"
            return
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            outcome.error = f"Claude Code failed: {detail[-1][:200] if detail else f'exit code {proc.returncode}'}"
            return
        if data.get("is_error"):
            outcome.error = f"Claude Code: {str(data.get('result') or data.get('subtype') or 'error')[:200]}"
            return
        usage = data.get("usage") or {}
        in_tok = int(usage.get("input_tokens") or 0) + int(usage.get("cache_read_input_tokens") or 0) + int(usage.get("cache_creation_input_tokens") or 0)
        out_tok = int(usage.get("output_tokens") or 0)
        outcome.calls += 1
        outcome.input_tokens += in_tok
        outcome.output_tokens += out_tok
        outcome.call_log.append((f"claude-code/{self.model}", len(items), in_tok, out_tok, data.get("stop_reason") or "end_turn", 0.0))
        result = data.get("structured_output")
        if result is None:
            try:
                result = json.loads(data.get("result") or "{}")
            except json.JSONDecodeError:
                log.warning("Unparseable Claude Code output: %.200s", data.get("result"))
                return
        _collect(result, items, names, outcome)
        log.info("Claude Code categorized %d/%d payees in %.1fs", len(outcome.results), len(items), time.monotonic() - started)


class ChainClassifier:
    """Try each backend in turn; items a failing backend couldn't answer go to the next one."""

    def __init__(self, backends: list):
        self.backends = backends

    def __call__(self, items: list[LLMItem], categories: list[dict], examples: list[tuple[str, str]]) -> LLMBatchOutcome:
        total = LLMBatchOutcome()
        remaining = list(items)
        errors: list[str] = []
        for backend in self.backends:
            if not remaining:
                break
            out = backend(remaining, categories, examples)
            total.results.update(out.results)
            total.calls += out.calls
            total.input_tokens += out.input_tokens
            total.output_tokens += out.output_tokens
            total.cost_usd += out.cost_usd
            total.call_log.extend(out.call_log)
            if out.calls and not total.backend:
                total.backend = out.backend
            if not out.error:
                break  # this backend worked; anything it left open is genuinely unknown
            errors.append(f"{getattr(backend, 'label', 'Claude')}: {out.error}")
            remaining = [it for it in remaining if it.id not in out.results]
        if errors and not total.calls:
            total.error = "; ".join(errors)
        return total


# ── Backend selection ─────────────────────────────────────────────────────────

MODES = {
    "auto": "Claude API if a key is set, then your Claude subscription, then the offline categorizer",
    "api": "Claude API only (ANTHROPIC_API_KEY), then the offline categorizer",
    "claude_code": "Your Claude subscription via the Claude Code CLI, then the offline categorizer",
    "local": "Offline categorizer only (nothing leaves this computer)",
}

_instances: dict[str, object] = {}


def _backend(kind: str):
    if kind not in _instances:
        _instances[kind] = HaikuClassifier() if kind == "api" else ClaudeCodeClassifier()
    return _instances[kind]


def api_key_set() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"))


def build_classifier(mode: str) -> Classifier | None:
    backends = []
    if mode == "api" or (mode == "auto" and api_key_set()):
        backends.append(_backend("api"))
    if mode == "claude_code" or (mode == "auto" and shutil.which("claude")):
        backends.append(_backend("claude_code"))
    return ChainClassifier(backends) if backends else None


def default_classifier(conn=None) -> Classifier | None:
    from tracker import db  # local import: llm.py stays importable without a DB

    mode = "auto"
    if conn is not None:
        mode = db.get_setting(conn, "categorizer", "auto")
    return build_classifier(mode)


# ── One structured answer (used by the money plan) ────────────────────────────


def structured_completion(system: str, user: str, schema: dict, model: str, mode: str = "auto",
                          effort: str = "medium", timeout: int = 300) -> tuple[dict | None, str | None, list[str]]:
    """Ask for one JSON answer matching `schema`, trying the API and then your subscription.

    Returns (data, label of the backend that answered, errors from backends that didn't).
    """
    errors: list[str] = []
    if mode == "api" or (mode == "auto" and api_key_set()):
        data, err = _api_json(system, user, schema, model, effort)
        if data is not None:
            return data, HaikuClassifier.label, errors
        errors.append(f"{HaikuClassifier.label}: {err}")
    if mode == "claude_code" or (mode == "auto" and shutil.which("claude")):
        data, err = _cli_json(system, user, schema, model, timeout)
        if data is not None:
            return data, ClaudeCodeClassifier.label, errors
        errors.append(f"{ClaudeCodeClassifier.label}: {err}")
    return None, None, errors


def _api_json(system: str, user: str, schema: dict, model: str, effort: str) -> tuple[dict | None, str | None]:
    try:
        client = anthropic.Anthropic()
        response = client.beta.messages.create(
            model=model,
            max_tokens=16000,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",  # if this model's safeguards decline, the API retries on a suitable model
        )
    except anthropic.APIStatusError as exc:
        msg = str(exc.message)
        return None, "no API credits left" if "credit balance" in msg.lower() else f"error {exc.status_code}: {msg[:160]}"
    except anthropic.APIConnectionError:
        return None, "could not reach the API"
    except (TypeError, anthropic.AnthropicError) as exc:
        return None, NO_CREDENTIALS if "authentication" in str(exc).lower() else f"error: {str(exc)[:160]}"
    if response.stop_reason == "refusal":
        return None, "Claude declined to answer"
    if response.stop_reason == "max_tokens":
        return None, "the answer was cut off"
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        return json.loads(text), None
    except json.JSONDecodeError:
        return None, "unreadable answer"


def _cli_json(system: str, user: str, schema: dict, model: str, timeout: int) -> tuple[dict | None, str | None]:
    exe = shutil.which("claude")
    if not exe:
        return None, "Claude Code CLI not found"
    cmd = [
        exe, "-p", "--model", model, "--output-format", "json", "--json-schema", json.dumps(schema),
        "--system-prompt", system, "--tools", "", "--strict-mcp-config", "--setting-sources", "", "--no-session-persistence",
    ]
    try:
        proc = subprocess.run(
            cmd, input=user, capture_output=True, text=True, encoding="utf-8", timeout=timeout,
            cwd=tempfile.gettempdir(), env=ClaudeCodeClassifier.environment(),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0,
        )
    except FileNotFoundError:
        return None, "Claude Code CLI not found"
    except subprocess.TimeoutExpired:
        return None, f"no answer within {timeout}s"
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        lines = (proc.stderr or proc.stdout or "").strip().splitlines()
        return None, (lines[-1][:200] if lines else f"exit code {proc.returncode}")
    if data.get("is_error"):
        return None, str(data.get("result") or data.get("subtype") or "error")[:200]
    result = data.get("structured_output")
    if result is None:
        try:
            result = json.loads(data.get("result") or "")
        except json.JSONDecodeError:
            return None, "unreadable answer"
    return result, None


_cli_status: tuple[float, dict] | None = None


def claude_code_status(refresh: bool = False) -> dict:
    """Is the CLI installed and signed in? Cached for 5 minutes (it takes a second or two)."""
    global _cli_status
    if _cli_status and not refresh and time.monotonic() - _cli_status[0] < 300:
        return _cli_status[1]
    exe = shutil.which("claude")
    status = {"installed": bool(exe), "logged_in": False, "auth_method": None, "error": None}
    if exe:
        try:
            proc = subprocess.run(
                [exe, "auth", "status"], capture_output=True, text=True, encoding="utf-8", timeout=30,
                env=ClaudeCodeClassifier.environment(), cwd=tempfile.gettempdir(),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0,
            )
            info = json.loads(proc.stdout or "{}")
            status.update(logged_in=bool(info.get("loggedIn")), auth_method=info.get("authMethod"))
        except Exception as exc:
            status["error"] = str(exc)[:200]
    _cli_status = (time.monotonic(), status)
    return status
