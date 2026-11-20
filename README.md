# Expense Tracker

A private, local expense tracker for Indian bank accounts (HDFC Bank and City Union Bank, and most other banks' statement formats). It turns your statements into categorized spending, learns from every correction you make, and gives you the statistics and a money plan to act on.

Everything runs on your own computer at `http://127.0.0.1:8765`. Your data lives in `data/expenses.db` and never leaves the machine, apart from the small, masked pieces described under [Privacy](#privacy).

## What it does

- **Imports statements:** Excel (`.xls`/`.xlsx`), CSV or PDF, including password-protected PDFs. You can drag them in, fetch them through [assisted net banking](#fetch-from-net-banking-assisted-login), or pull them from [Gmail](#gmail-optional-read-only). Overlapping statements never double-count.
- **Reads credit card statements too**, so purchases made on the card count as spending and paying the bill doesn't. Each card's bill (amount due, minimum, due date) shows on the dashboard, marked paid or overdue. See [Credit cards](#credit-cards).
- **Recognises the payee** in UPI, card, NEFT, IMPS, auto-debit (ACH/NACH), ATM and bank-charge narrations. So "Swiggy" is Swiggy whether you paid by UPI, card or a different UPI ID.
- **Categorizes automatically** into 29 categories and 34 sub-categories (Food & Dining → Food Delivery / Restaurants & Cafes, Transport → Cabs & Autos / Public Transport / Vehicle Upkeep, ...):
  - your own rules first, then about 300 built-in keywords;
  - then Claude for payees it hasn't seen (one call per import);
  - then an offline guesser when Claude isn't available.
- **Learns from you.** Change a category and choose "all from this payee": every past transaction from that payee updates, and future ones follow. Or change just one transaction.
- **Spots transfers** between your own accounts and credit-card bill payments, and leaves them out of spending.
- **Where the money went**, at the top of the dashboard. All the money that went out in the period, ranked: each category, investments, card bills, and transfers to accounts the app doesn't track. Next to it, what came in, and the change in your bank balance it adds up to, so every rupee is accounted for. One-off payments (large, and not to someone you pay regularly) are set apart from regular spending. What the app can't explain yet is shown openly: card bills without a card statement, payments to people, Uncategorized and Other, and likely mix-ups such as a rent payment filed under Parking because its note said "parking". Each has a link to fix it.
- **Dashboard:** spent, income, saved and invested (with rates); the needs / wants / saved split; each category against your usual; regular commitments (rent, EMIs, SIPs, subscriptions); top merchants; payments to people; unusually large payments; a 12-month trend. You can look at a month, 3 months, the financial year (April–March) or 12 months.
- **Money plan:** Claude reads your monthly totals and suggests concrete actions with estimated monthly savings, budgets you can adopt in one click, and an emergency-fund target. There's an offline version too.
- **Budgets** per category, with progress shown on the dashboard.
- **Claude Desktop integration (MCP):** ask "how much did I spend on food last month?" or "what's my savings rate?", and fix categories from chat.

## Quick start

You need Windows with Python 3.12 (via [uv](https://docs.astral.sh/uv/)) and Microsoft Edge or Google Chrome.

```powershell
uv venv --python 3.12 .venv
```

```powershell
uv pip install --python .venv\Scripts\python.exe -r requirements.txt
```

```powershell
.venv\Scripts\python.exe app.py
```

Open http://127.0.0.1:8765, then:

1. **Set up sign-in:** the first time, the app asks for your name and a PIN (4 to 8 digits). Your existing data becomes yours.
1. **Settings → Add an account** for each bank account. Enter the **last 4 digits**, so imports find the right account, and **your name as it appears on the account**, so transfers to yourself are recognised.
2. **Import:** drop a statement in, or use **Fetch from net banking**.
3. **Review:** confirm or fix anything the app was unsure about. Each fix teaches it.
4. **Dashboard** and **Plan:** see where your money goes and what to change.

### Claude for categorization (optional, recommended)

New payees are categorized by Claude Haiku 5.5, the cheapest Claude model. Two ways to connect it, chosen automatically (or pick one in **Settings → Categorization**):

| Route | Setup | Cost |
|---|---|---|
| **Your Claude subscription** (Pro/Max) | Install Claude Code and sign in once by running `claude`. | Counts toward your plan's usage limits |
| **Claude API key** | Copy `.env.example` to `.env` and set `ANTHROPIC_API_KEY` (key needs credits). | About $0.001 per import of 30 new payees |

Without either, the offline guesser and the built-in rules still work, and nothing leaves your computer. To check your setup with one real call (well under $0.01):

```powershell
.venv\Scripts\python.exe scripts\smoke_claude.py
```

## Using the app

### Categories and corrections

A transaction gets its category from the first of these that applies:

1. Your one-off choice for that transaction.
2. Your rules: corrections you made ("all from this payee") and rules you added on the **Rules** page ("narration contains ZOMATO → Food Delivery").
3. Built-in keyword rules (Swiggy, Zomato, Uber, IRCTC, TANGEDCO, Airtel, SIPs, ATM...).
4. Claude's earlier answer for this payee (each payee is asked about only once).
5. Claude, for payees not seen before.
6. The offline guesser: learns from your corrections and from business words (MESS, TAILORS, MEDICALS, a "RENT" note...).
7. Uncategorized, flagged for **Review**.

Low-confidence results land in **Review**. Accepting one turns it into your own rule. **Categorize again** retries Claude for uncategorized transactions and offline guesses.

In **Categories** you can rename categories, add sub-categories, merge categories, hide ones you don't use, and choose whether each is a **need**, a **want** or **savings**. That choice drives the 50/30/20 split.

### On your phone (Android)

The app works on a phone screen. There's a menu along the bottom (**Home**, **Payments**, **Add**, **Check**, **More**), text is larger, and payments show as cards.

- **Install it.** Open the app's address in Chrome and choose **⋮ → Add to Home screen** (or **Install app**). It then opens full screen from its own icon like any other app. Installing needs the private HTTPS address from Tailscale (phase 5 of the plan); on the computer itself, `localhost` works too.
- **Add a statement:**
  - **Add → Choose a statement** picks a PDF or Excel file from Downloads or Drive.
  - **Share:** in WhatsApp, Gmail or Files, tap **Share** on the statement and pick **Expenses**. It's imported straight away.
- **Fix a category.** Tap the category on a payment for a big, searchable list with your recently used categories first. Then choose "all from this payee" or "just this one". Payments the app is unsure about are highlighted, and **Check** shows them all.
- **Net banking** fetch stays on the computer, because the bank's window opens there.

Nothing financial is stored on the phone. The app keeps only an "app not reachable" page for when the computer is off. A shared statement waits on the phone only until it has been uploaded.

### தமிழ் / Tamil

Everyone can use the app in Tamil. The setting is under **Settings → People → Language**, or **More → தமிழ்** on a phone. It's saved for each person, so a parent's phone opens in Tamil while yours stays in English.

- **What switches to Tamil:** menus, buttons, category names, dates and month names, the "Where the money went" and "What stands out" sentences, and sign-in messages.
- **What stays as written:** payee names and bank narrations stay exactly as the bank wrote them, and amounts keep the ₹1,23,456 style.
- **Where the wording lives:** all the Tamil is in one file, `static/i18n/ta.json`, keyed by the English text, so it's easy to review or correct. It's a draft, so have a Tamil reader check it. Anything without a Tamil entry shows in English.

### Family members: a private space each

Several people can use the app, each in a separate space with their own accounts, statements, categories, budgets and plan. Nobody can see anyone else's.

- **Adding someone:** **Settings → People → Add a person** with their name and a PIN you agree on. Only the person who set up the app can add people.
- **Signing in:** everyone signs in by tapping their name and typing their PIN, on this computer or on a phone (see Phase 5 in the plan: a private Tailscale link).
- **Lockout:** five wrong PINs in a row lock that person out for 10 minutes. **Settings → People** lets you change your PIN, which signs you out on your other devices. The owner can reset anyone's PIN.
- **Claude for other people's spaces:** your Claude subscription only categorizes your own data, because a personal plan can't be used to serve other people. Other people's spaces use a Claude API key (`.env`) if there is one, otherwise the offline categorizer.
- **Net banking:** fetching opens a browser window on the computer running the app. Only the person who opened it sees its status.
- **Gmail and PDF passwords** are kept per person too.

### Credit cards

When you pay a credit card bill from your bank account, the app counts it as a transfer, not spending. Otherwise every purchase would be counted twice. What you bought with the card only shows up once you import the **card statement**:

1. Drop the card statement (PDF or Excel) on the **Import** tab, or fetch it through net banking or Gmail like a bank statement. Card PDFs are usually password-protected; the app asks for the password once and can remember it.
2. The app recognises a card statement by its bill box: payment due date, minimum amount due, credit limit. The first time, it adds the card as an account of its own ("HDFC Credit Card"), going by the last 4 digits printed on the statement. You can rename it in **Settings**. To set the account up yourself, add an account of type **Credit card** with the card's last 4 digits.
3. Purchases on the card are categorized like any other payment. Refunds and cashback count as income. Fees, finance charges and GST go to Bank Charges. The bill payment on the card ("PAYMENT RECEIVED", "NETBANKING TRANSFER", "AUTOPAY"...) is matched with the same amount leaving your bank account within 5 days, and both are marked **Credit Card Payment**.

The dashboard then shows:

- **Credit card bills:** each card's latest bill, the minimum due and the due date. It also shows how much you've paid since the statement (from the card itself or from your bank statement) and whether the bill is due, paid, part-paid or overdue.
- **Spending on cards:** how much of the period's spending went on cards.
- **A warning when card bills have no card statement behind them.** Such bills mean real spending is missing from the totals and the savings rate looks better than it is. The money plan is told about this too.

Net banking keeps separate recorded steps for a bank's card statements, because they're on a different page from the account statement.

### Fetch from net banking (assisted login)

**Import → Fetch from net banking**, pick the account, then **Open net banking**:

1. A browser window opens at the bank's login page. It's your Edge or Chrome, with a fresh profile that's thrown away afterwards.
2. **You log in yourself** (user ID, password, OTP, captcha). The app never reads or stores what you type there.
3. Download a statement as usual and it's imported automatically.
4. To automate the next time, click **Record my steps** after logging in, then go to the statement and download it.

   Next time: log in, choose a period ("since the last transaction", last 30 or 90 days, or custom dates), and click **Fetch statement**. The app repeats your clicks with the new dates.

**Logging out.** Closing the window alone isn't enough: the bank keeps your session alive until it times out. So the app logs out first. After **Fetch statement** imports a statement, it clicks the bank's Log out button, answers the "Are you sure?" prompt, checks that the bank shows you as logged out, and closes the window. **Log out and close** does the same when you downloaded by hand, and so does the 20-minute idle timeout. If the app can't find the Log out button, the window stays open and the button changes to **Close anyway**. Log out yourself in that case; the app remembers where you clicked for next time.

The recorder keeps only clicks, dropdown choices and date fields. It ignores login, password, OTP, PIN, card and phone fields, and clicks inside calendar pop-ups. It finds fields by their visible label, so it copes with pages whose internal IDs change, and month dropdowns follow the new period. If a bank changes its site, the app says which step failed. You can finish by hand (the download still imports) and record again.

Default login pages are `now.hdfc.bank.in` (HDFC) and `online.cub.bank.in` (CUB's "All in One"). Both are editable in the panel. The app adds no anti-bot workarounds. If a bank refuses an automated browser window, download the statement normally and drop it on the Import tab. Automated access may go against your bank's terms. Access through a bank's mobile-app MPIN is deliberately not supported.

### Money plan

**Plan → Create my plan with Claude** uses your last 3 complete months. You get:

- a summary and a health rating, with your needs/wants/saved split against a target;
- prioritised actions with the monthly amount each could free up;
- suggested monthly budgets ("Use these as my budgets");
- an emergency-fund target (about 6 months of essentials) and how to build it;
- things to keep an eye on.

**Offline plan** builds a simpler, rule-based version that sends nothing. The plan uses Claude Opus through the same route as categorization. This is general budgeting guidance based only on your transactions, not professional financial advice. It never recommends specific investment products; for those decisions, talk to a SEBI-registered adviser.

### Gmail (optional, read-only)

1. In [Google Cloud Console](https://console.cloud.google.com/), create a project and enable the **Gmail API**.
2. Under **OAuth consent screen**, choose External and add yourself as a test user.
3. Create credentials of type **OAuth client ID → Desktop app**. Download the JSON and save it as `data/credentials.json`.
4. In **Settings → Gmail**, click **Connect Gmail** and approve read-only access.
5. For each account, set **Gmail search** to a query that finds that bank's statement emails, for example `from:(bank-sender@example.com) subject:(statement)`.
6. Save the account's **PDF password** (kept in Windows Credential Manager), then click **Fetch statements now**.

### Claude Desktop (MCP)

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

| Tool | What it does |
|---|---|
| `where_money_went` | Every rupee in and out for a period, one-off payments, and what isn't explained yet |
| `spending_overview` | Savings rate, needs/wants, change vs usual, commitments, key facts |
| `spending_summary` | Totals by category, merchant, month or account |
| `search_transactions` | Find transactions by text, category, date or amount |
| `review_queue` | Low-confidence and uncategorized transactions |
| `recategorize` | Fix a category (teaches the tracker, like the web app) |
| `add_rule` | "Narration contains X → category" |
| `list_categories`, `list_accounts` | Reference data |
| `card_bills` | Each credit card's latest bill: amount due, due date, paid so far, status |
| `money_plan` | The latest money plan |

## Privacy

- **Categorization:** Claude sees the payee name, the UPI ID with digits masked, payment channel, debit/credit, and the narration with every number masked. Never amounts, balances, account or reference numbers.
- **Money plan:** Claude sees monthly totals per category, your savings rate, regular commitments, and the names of businesses you spend most with. People's names, individual transactions and account numbers are never included.
- **Net banking:** you log in yourself. Nothing you type there is read or stored.
- **PDF passwords** are kept in Windows Credential Manager, not in the database.
- **Logs** (`data/logs/`) record what happened (requests, imports, errors), never amounts, narrations, names or search text.
- **Sign-in:** PINs are stored only as salted scrypt hashes in `data/profiles.db`. Sessions are random tokens in an HttpOnly cookie, and only their SHA-256 is stored. Every data request needs a signed-in person and only ever touches that person's folder. First-time set-up only works on the computer itself, never through a relay.

## Files and folders

| Path | Contents |
|---|---|
| `data/expenses.db` | Your database (SQLite). Back it up by copying the file. |
| `data/profiles.db` | Who can sign in: names, PIN hashes, sessions |
| `data/profiles/<name>/` | Everyone else's space: their own `expenses.db`, `inbox/` and Gmail sign-in |
| `data/expenses-backup-*.db` | Automatic copies made before big upgrades |
| `data/inbox/` | Statements fetched by net banking (`netbanking/`), Gmail (`gmail/`) or uploaded (`uploads/`) |
| `data/logs/app.log`, `mcp.log` | Backend logs, rotated at 1 MB (5 kept) |
| `.env` | Optional `ANTHROPIC_API_KEY` and overrides (see `.env.example`) |

`data/` and `.env` are gitignored and never committed.

## Troubleshooting

- **Something failed in the app:** look at the end of `data/logs/app.log`. Errors include the full traceback.
- **A statement won't import:** the message names the columns it looked for (Date, Narration/Description, Withdrawal/Deposit or Amount + Dr/Cr). Password-protected PDFs ask for the password.
- **New payees stay Uncategorized:** check **Settings → Categorization** (API key credits, or `claude` signed in). They're still guessed offline in the meantime.
- **The page looks outdated after an update:** reload. Scripts are version-stamped, so a normal reload is enough.

## Development

```powershell
.venv\Scripts\python.exe -m pytest -q
```

```powershell
.venv\Scripts\ruff.exe check .
```

- Tests use synthetic statements and a fake net banking site only, never real data or the real Claude API.
- `CLAUDE.md` describes the architecture, conventions and privacy rules for working on the code with Claude Code.

### Claude Code setup in this repo

| Piece | What it does |
|---|---|
| Skill **`commit-push`** (`.claude/skills/commit-push/`) | Ask Claude to "commit and push". It runs tests and lint, checks nothing private is staged, writes the commit message, pushes and verifies. |
| Hook **post-push cleanup** (`scripts/hooks/post_push_cleanup.py`) | After Claude does a successful `git push`: removes Python/pytest/ruff caches and upload leftovers no statement refers to, and stops headless test browsers. Never touches imported statements, the database or logs. Writes to `.claude/logs/cleanup.log`. |
| Hook **audit log** (`scripts/hooks/audit_log.py`) | Records every tool call Claude makes in this project (tool, command or file, success) to `.claude/logs/claude-actions.jsonl`. Secrets are masked; file contents and typed text are never recorded. |

The hooks are registered in `.claude/settings.json` and run in the background. To turn them off, remove them from that file, or set `"disableAllHooks": true` in `.claude/settings.local.json`.

### Project layout

```
app.py                 web app (FastAPI)
mcp_server.py          Claude Desktop tools
config.py              categories, keyword rules, models, paths
tracker/               core: db, normalize, parsers, importer, categorizer, llm,
                       local_classifier, transfers, insights, advisor, service, logs,
                       fetch/ (netbanking, gmail, secrets)
templates/, static/    the single-page UI
scripts/               smoke_claude.py, hooks/
tests/                 pytest suite with synthetic data and a fake bank
```

### Known limits

- Statement layouts are recognised by their column headers. They're tested on real HDFC PDFs and on synthetic HDFC- and CUB-style files. CUB's real formats haven't been tested yet.
- Credit card statements are tested on synthetic files in HDFC's older and newer layouts, not on a real card statement yet.
- A card purchase converted to EMI shows up as the purchase plus the monthly EMIs, so it can be counted twice. Mark one side as you prefer.
- Transaction-alert emails (between statements) aren't parsed yet.
- Some merchant names arrive truncated by the bank ("Makemytrip India Pri"); that's display only.
