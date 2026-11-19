# Expense Tracker

A local expense tracker for HDFC and City Union Bank statements. It categorizes transactions from their narrations and learns from your corrections. Unknown payees go to **Claude Haiku 5.5** (`claude-haiku-5-5`, the cheapest Claude model). An **MCP server** lets you ask Claude Desktop about your spending.

Everything runs on your machine (`127.0.0.1` only), and the data lives in `data/expenses.db`.

## Setup

```powershell
uv venv --python 3.12 .venv
uv pip install --python .venv\Scripts\python.exe -r requirements.txt
copy .env.example .env      # optional: put an ANTHROPIC_API_KEY with credits in .env
.venv\Scripts\python.exe app.py
```

No API credits? If you have a Claude Pro/Max plan and Claude Code installed, sign in once with `claude`. The app then uses your subscription automatically. Without either, the offline categorizer still works.

Open http://127.0.0.1:8765. The steps after that:

1. **Settings → Add an account** for each HDFC / CUB account. Enter the **last 4 digits**, so imports pick the right account automatically. Enter **your name as on the account**, so transfers to yourself are recognised.
2. **Import**: drop Excel (.xls/.xlsx), CSV or PDF statements. For a password-protected PDF the app asks for the password and can remember it.
3. **Review**: fix anything Claude was unsure about.

To check your API key and see the real cost (one call, well under $0.01): `.venv\Scripts\python.exe scripts\smoke_claude.py`

## How categorization works

Each narration is reduced to a **payee key**: direction plus cleaned payee name. Some examples:

- `UPI-SWIGGY-swiggy@icici-…` becomes `D:SWIGGY`.
- `UPI/DR/…/SWIGGY/YESB/…` also becomes `D:SWIGGY`.
- A refund from Swiggy becomes `C:SWIGGY`, so it is kept separate from purchases.

Reference numbers, IFSC codes and card numbers are stripped out. If the payee name is a payment gateway (PayU, Razorpay…), the UPI ID is used instead.

The first match wins:

| Order | Source | Notes |
|---|---|---|
| 1 | **You, one-off** | A category you set on a single transaction. Never touched again. |
| 2 | **Your rules** | Created when you correct a category, or added on the Rules page. |
| 3 | **Built-in rules** | About 250 keywords: Swiggy, Zomato, Uber, IRCTC, TANGEDCO, ATM, SIP… |
| 4 | **Claude's earlier answer** | Cached per payee, so a payee is only ever sent once. |
| 5 | **Claude Haiku 5.5** | All new payees from an import go in one batched call. Results with confidence below 60% are flagged for review. |
| 6 | **Offline guess** | A small naive-Bayes model on the words in the payee name, UPI ID and UPI remark, trained on your corrections plus a built-in word list (MESS → Food, TAILORS → Personal Care…). Phone-number UPI IDs default to Payments to People. Not cached, so Claude can improve these later. |
| 7 | **Uncategorized** | Flagged for review. Use "Categorize again" to retry. |

**Which Claude is used** is set in **Settings → Categorization**:

- **Automatic** (the default) tries, in order: the Claude API (if `ANTHROPIC_API_KEY` is set), then your Claude subscription through the Claude Code CLI, then the offline guesser.
- **Claude via your subscription** runs the official `claude` CLI in print mode, signed in with your own Pro/Max account. It uses no tools, no MCP servers and no settings files, and nothing is saved as a session. It counts toward your plan's usage limits; there's no per-token bill. This is fine for your own use. If you ever host the app for other people, use an API key instead.
- **Claude API key** needs credits on console.anthropic.com.
- **Offline only** sends nothing anywhere.

If one route fails (no API credits, usage limit reached, no network), the next one takes over.

**Correcting a category** asks whether to apply it to *all transactions from that payee, now and in future* or *only this one*.

- **All from this payee** saves a rule. Every past transaction from that payee updates right away, and future imports follow the rule. Your recent corrections are also sent to Claude as examples, so new payees that resemble ones you've corrected get classified your way.
- **Accepting** Claude's pick in Review turns it into your own rule.

Transfers between your accounts (a debit in one matched by a credit in the other within 2 days), credit card bill payments and investments are kept out of spending totals.

**What is sent to Claude:** the payee name, UPI ID, channel, debit/credit, and the narration with every number masked. Amounts, balances, account numbers and reference numbers are never sent. A typical import of 30 new payees costs about $0.001.

## Fetch from net banking (assisted login)

**Import → Fetch from net banking**, pick the account, then click **Open net banking**:

1. A browser window opens at your bank's login page. It's your installed Edge or Chrome, with a fresh profile that's thrown away afterwards.
2. **You log in yourself** (user ID, password, OTP, captcha). The app never reads or stores any of it.
3. Download a statement as usual, Excel preferred. It's imported into the chosen account automatically. If the statement belongs to a different account number, it goes to the matching account instead.
4. **To make the next fetch automatic**, click **Record my steps** after logging in, before you start navigating. Then open the statement page, choose the account, period and format, and download. The recording stops by itself when the file arrives.

   Next time: log in, choose a period ("since the last transaction", last 30/90 days or custom dates), and click **Fetch statement**. The app replays your clicks with the new dates filled in.

The recorder keeps only clicks, dropdown choices and date fields. It ignores password, OTP, PIN, user-ID, card and phone fields, and clicks inside calendar pop-ups. If the bank changes its pages, the app says which step it couldn't find. You then finish by hand (the download is still imported) and record again.

The login page addresses are editable: the banks moved to RBI's `.bank.in` domains, so check that the default opens the real login page. The app adds no anti-bot workarounds. If a bank refuses to work in an automated browser window, download the statement in your normal browser and use the regular import. Automated access may also go against your bank's terms of use.

MPIN-based access to the banks' mobile apps is intentionally not supported. An MPIN is tied to the bank's app on your registered phone, and using it elsewhere would mean defeating the bank's device binding.

## Gmail (optional, read-only)

1. In [Google Cloud Console](https://console.cloud.google.com/), create a project and enable the **Gmail API**.
2. Under **APIs & Services → OAuth consent screen**, choose External and add yourself as a test user.
3. Create credentials of type **OAuth client ID → Desktop app**. Download the JSON and save it as `data/credentials.json`.
4. In **Settings → Gmail**, click **Connect Gmail** and approve read-only access in the browser.
5. For each account, fill in **Gmail search** with a query that finds that bank's statement emails. For example: `from:(bank-sender@example.com) subject:(statement)`. Copy the sender address from one of your real statement emails.
6. Save the account's **PDF password** (stored in Windows Credential Manager), then click **Fetch statements now**.

## Claude Desktop (MCP)

Add this to `%APPDATA%\Claude\claude_desktop_config.json` and restart Claude Desktop:

```json
{
  "mcpServers": {
    "expense-tracker": {
      "command": "E:/Fun/expense-tracker/.venv/Scripts/python.exe",
      "args": ["E:/Fun/expense-tracker/mcp_server.py"]
    }
  }
}
```

Tools: `spending_summary`, `search_transactions`, `list_categories`, `review_queue`, `list_accounts`, `recategorize`, `add_rule`. A correction made through Claude Desktop teaches the tracker exactly like one made in the web UI.

Example question: *"How much did I spend on food in October, and which payees were the biggest?"*

## Development

```powershell
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\ruff.exe check .
```

Tests use synthetic statements only and never call the real Claude API.

### Known limits

- Statement layouts are found by their column headers (Date / Narration or Description / Withdrawal & Deposit, or Amount + Dr/Cr), not hard-coded per bank. They are tested against synthetic HDFC- and CUB-style files. If a real file fails to import, the error names the columns it looked for. Share just the header row and a redacted line to get the parser adjusted.
- Text-only PDFs (no table lines) work out debit vs credit from the running balance.
- Transaction-alert emails (near-real-time tracking between statements) are not parsed yet. Only statement attachments are.
