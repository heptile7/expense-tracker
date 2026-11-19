"""One real Claude Haiku 5.5 call on 10 synthetic payees, to check your API key and see the cost.

Run from the project folder after putting ANTHROPIC_API_KEY in .env:
    .venv\\Scripts\\python.exe scripts\\smoke_claude.py
Costs well under $0.01. Uses no data from your database.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))

import config  # noqa: E402
from tracker.llm import HaikuClassifier, LLMItem  # noqa: E402
from tracker.normalize import normalize  # noqa: E402

SAMPLES = [
    ("UPI-SRI MURUGAN TRADERS-Q123456789@YBL-YESB0YBLUPI-412345678904-UPI", "debit"),
    ("UPI-MAHALAKSHMI MESS-MAHALAKSHMIMESS@OKICICI-ICIC0000001-412345678905-UPI", "debit"),
    ("UPI/DR/412345678901/KAVITHA TAILORS/SBIN/kavithatailors@oksbi/Stitching", "debit"),
    ("UPI/DR/412345678902/GANESH FLOWER STALL/YESB/ganeshflowers@ybl/Pooja", "debit"),
    ("UPI-VIGNESH M-9840012345@OKAXIS-UTIB0000002-412345678906-SNACKS", "debit"),
    ("NEFT CR-SBIN0001234-ACME TECHNOLOGIES PVT LTD-TEST USER-SBINN52024040312345", "credit"),
    ("UPI-LENSKART SOLUTIONS-LENSKART@ICICI-ICIC0DC0099-412345678907-UPI", "debit"),
    ("UPI/DR/412345678903/TANGEDCO/SBIN/tangedco@sbi/EB BILL", "debit"),
    ("ACH D- TATA CAPITAL FINANCIAL-1234567", "debit"),
    ("UPI-PRAKASH AUTO WORKS-PRAKASHAUTO@OKSBI-SBIN0001234-412345678908-SERVICE", "debit"),
]

categories = [{"name": n, "kind": k, "description": d} for n, k, _c, d in config.DEFAULT_CATEGORIES if n != config.UNCATEGORIZED]
items = []
for i, (narr, direction) in enumerate(SAMPLES):
    n = normalize(narr, direction)
    items.append(LLMItem(i, direction, n.channel, n.merchant_key.partition(":")[2], n.vpa, n.llm_hint))

out = HaikuClassifier()(items, categories, [("debit | RAMESH KUMAR", "Rent")])
if out.error:
    sys.exit(f"Failed: {out.error}")
for it in items:
    r = out.results.get(it.id)
    print(f"{it.payee:32} -> {r.category if r else '(no answer)':30} {f'{r.confidence:.2f}' if r else ''}")
print(f"\n{out.calls} call(s), {out.input_tokens} input + {out.output_tokens} output tokens, ${out.cost_usd:.5f}")
