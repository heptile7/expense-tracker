"""Claude Haiku 5.5 categorizer: one request per batch of unique, unknown merchants.

Only masked narration text, the payee name / UPI ID, channel and direction are sent.
Amounts, balances, account numbers and reference numbers never leave the machine.
"""

import json
import logging
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
    upi_id: str | None
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
    # (model, n_items, input_tokens, output_tokens, stop_reason) per API call, written to llm_calls by the caller
    call_log: list[tuple] = field(default_factory=list)


# (items, categories, examples) -> outcome. Tests inject a fake with this signature.
Classifier = Callable[[list[LLMItem], list[dict], list[tuple[str, str]]], LLMBatchOutcome]

NO_CREDENTIALS = "Claude isn't set up: add ANTHROPIC_API_KEY to .env and restart. New payees stay Uncategorized until then."

SYSTEM_PROMPT = """You categorize transactions from Indian bank statements for a personal expense tracker.

Each item has the payee/payer name, UPI ID if any, payment channel, direction (debit = money out,
credit = money in) and the narration with numbers masked as '#'. Use them to work out what kind of
merchant or person this is, then choose exactly one category from the list for every item.

Guidance:
- Well-known Indian merchants, apps and billers: use what you know about them (UPI IDs often name the merchant).
- A debit to what looks like an individual's name with no other clue: "Payments to People".
- A credit from an individual with no other clue: "Other Income".
- Small shops (stores, traders, enterprises) with no clear type: pick the most likely category with lower confidence.
- If you genuinely cannot tell, answer "Uncategorized".
- The user's own past decisions are listed as examples; follow their preferences for similar payees.

confidence: 0.9-1.0 when the merchant is well known or the name is unambiguous, 0.6-0.8 for a reasonable
inference, below 0.6 when guessing."""


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


def _user_message(items: list[LLMItem], categories: list[dict], examples: list[tuple[str, str]]) -> str:
    cat_lines = "\n".join(f"- {c['name']} ({c['kind']}): {c.get('description') or ''}".rstrip(": ") for c in categories)
    parts = [f"Categories:\n{cat_lines}"]
    if examples:
        ex_lines = "\n".join(f"- {payee} -> {cat}" for payee, cat in examples)
        parts.append(f"The user's own categorizations (follow these preferences):\n{ex_lines}")
    payload = [
        {
            "id": it.id,
            "direction": it.direction,
            "channel": it.channel,
            "payee": it.payee,
            "upi_id": it.upi_id,
            "narration": it.narration,
        }
        for it in items
    ]
    parts.append("Transactions to categorize:\n" + json.dumps(payload, ensure_ascii=False, indent=1))
    return "\n\n".join(parts)


class HaikuClassifier:
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

    def __call__(
        self, items: list[LLMItem], categories: list[dict], examples: list[tuple[str, str]]
    ) -> LLMBatchOutcome:
        outcome = LLMBatchOutcome()
        client = self._get_client()
        if client is None:
            outcome.error = self._init_error
            return outcome
        names = [c["name"] for c in categories]
        if config.UNCATEGORIZED not in names:
            names.append(config.UNCATEGORIZED)
        for start in range(0, len(items), config.LLM_BATCH_SIZE):
            if self._init_error:
                outcome.error = self._init_error
                break
            self._classify(client, items[start : start + config.LLM_BATCH_SIZE], categories, names, examples, outcome)
        return outcome

    def _classify(
        self,
        client: anthropic.Anthropic,
        items: list[LLMItem],
        categories: list[dict],
        names: list[str],
        examples: list[tuple[str, str]],
        outcome: LLMBatchOutcome,
    ) -> None:
        try:
            response = client.messages.create(
                model=self.model,
                max_tokens=config.LLM_MAX_TOKENS,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": _user_message(items, categories, examples)}],
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": _schema(names)}},
            )
        except anthropic.RateLimitError as exc:
            outcome.error = f"Rate limited by the Claude API, try again shortly ({exc.status_code})"
            return
        except anthropic.AuthenticationError:
            outcome.error = "Claude API key is invalid: check ANTHROPIC_API_KEY in .env"
            return
        except anthropic.APIStatusError as exc:
            outcome.error = f"Claude API error {exc.status_code}: {exc.message}"
            return
        except anthropic.APIConnectionError:
            outcome.error = "Could not reach the Claude API (network error)"
            return
        except TypeError as exc:  # SDK raises this when no API key / auth is configured
            if "authentication" not in str(exc).lower():
                raise
            self._init_error = outcome.error = NO_CREDENTIALS
            self._client = None
            return
        except anthropic.AnthropicError as exc:
            outcome.error = f"Claude API error: {exc}"
            return

        usage = response.usage
        outcome.calls += 1
        outcome.input_tokens += usage.input_tokens
        outcome.output_tokens += usage.output_tokens
        outcome.cost_usd += (
            usage.input_tokens * config.PRICE_INPUT_PER_MTOK + usage.output_tokens * config.PRICE_OUTPUT_PER_MTOK
        ) / 1_000_000
        outcome.call_log.append((self.model, len(items), usage.input_tokens, usage.output_tokens, response.stop_reason))

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
        if not text:
            return
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            log.warning("Unparseable categorizer output: %.200s", text)
            return
        valid_ids = {it.id for it in items}
        for r in data.get("results", []):
            if r.get("id") in valid_ids and r.get("category") in names:
                conf = max(0.0, min(1.0, float(r.get("confidence", 0.5))))
                outcome.results[r["id"]] = LLMResult(r["category"], conf)


_default: HaikuClassifier | None = None


def default_classifier() -> HaikuClassifier:
    global _default
    if _default is None:
        _default = HaikuClassifier()
    return _default
