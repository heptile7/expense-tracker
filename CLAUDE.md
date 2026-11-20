# CLAUDE.md

Guidance for Claude Code working in this repository. User-facing docs are in `README.md`.

## What this is

A local, single-user expense tracker for Indian bank accounts (HDFC, City Union Bank) and credit cards. It imports bank and card statements (Excel/CSV/PDF upload, assisted net banking download, Gmail attachments), works out a payee for every transaction, categorizes it (rules → Claude → offline guess), learns from the user's corrections, and shows statistics plus a money plan. FastAPI backend + vanilla JS page, SQLite, an MCP server for Claude Desktop. Runs on Windows; the user's shell is PowerShell.

## Commands

```powershell
uv venv --python 3.12 .venv                                        # first time only
uv pip install --python .venv\Scripts\python.exe -r requirements.txt
.venv\Scripts\python.exe app.py                                    # http://127.0.0.1:8765
.venv\Scripts\python.exe -m pytest -q                              # ~40 s, all must pass
.venv\Scripts\ruff.exe check .                                     # must be clean
.venv\Scripts\python.exe mcp_server.py                             # MCP server (stdio)
.venv\Scripts\python.exe scripts\smoke_claude.py                   # one real Claude call
```

- In the desktop app, start the server with the Browser pane's `preview_start` using the `expense-tracker` config in `.claude/launch.json`.
- Python changes need a server restart. Static files don't: `index.html` stamps `app.js`/`style.css` with their mtime.

## Code map

| Path | Role |
|---|---|
| `app.py` | FastAPI routes, request logging middleware, error handlers. Thin: calls `tracker/service.py`. |
| `mcp_server.py` | MCP tools for Claude Desktop (MCP SDK v2: `from mcp.server.mcpserver import MCPServer`). Same service layer as the web app. |
| `config.py` | Paths (overridable via `EXPENSE_TRACKER_DATA`/`_DB`), model IDs, default categories, sub-categories, needs/wants groups, seed keyword rules, local lexicon. |
| `tracker/db.py` | Schema, migrations (`PRAGMA user_version`, now 4), seeding (`SEED_RULES_VERSION`), `category_tree()`, `get_setting`/`set_setting`. |
| `tracker/normalize.py` | Narration → channel, payee, UPI ID, **merchant key** (`D:SWIGGY` / `C:ACME TECHNOLOGIES`). |
| `tracker/parsers/` | Statement readers. `table_sniff.py` finds the table by header names; `pdf_text.py` is the bank text fallback (direction from running balance); `card_text.py` reads card statements (Cr / `+` / trailing C credits, `C` rupee glyph, reward points) and the bill box. `ParsedStatement.kind` is `bank` or `credit_card`. |
| `tracker/importer.py` | Parse → pick account (a card statement goes to a card account, created from its last 4 digits if needed) → fingerprint dedupe (identical rows in one file get an occurrence number) → insert → card payments → categorize → transfers. |
| `tracker/categorizer.py` | Priority pipeline (below). `categorize(conn, ids, classifier, use_llm, use_local)`. |
| `tracker/llm.py` | Claude routes: `HaikuClassifier` (API), `ClaudeCodeClassifier` (the `claude` CLI on the user's subscription), `ChainClassifier`, `structured_completion` (money plan). |
| `tracker/local_classifier.py` | Offline naive Bayes on payee/UPI/remark words. |
| `tracker/transfers.py` | Self-transfer and card-payment detection. Card purchases never pair; a card credit pairs with a bank debit within ±5 days only if it looks like a bill payment (`looks_like_card_payment`). |
| `tracker/insights.py` | Dashboard statistics, recurring commitments, unusual payments, card bills (`card_bills`) and bill payments with no card statement (`card_bills_not_itemised`), plain-language facts. |
| `tracker/flow.py` | "Where the money went": money in/out over a period reconciled to the bank balance change (internal transfers and itemised card bills left out), one-offs, what's not explained yet, likely mix-ups. `/api/where`, MCP `where_money_went`. |
| `tracker/advisor.py` | Money plan: totals-only snapshot → Claude (Opus) or offline rules; budgets. |
| `tracker/service.py` | Shared operations for UI and MCP (categories, accounts, rules, budgets, corrections, summaries). |
| `tracker/profiles.py` | People and sign-in. Registry `data/profiles.db` (scrypt PIN hashes, lockout, session token hashes). Each person has a data folder (owner: `data/`, others: `data/profiles/<slug>/`). `profiles.use(p)` sets a ContextVar that `db.connect()`, inbox, Gmail token and keyring keys follow. `app.SignInMiddleware` activates the signed-in person per request; outside requests (tests, MCP) everything is the owner's. |
| `tracker/fetch/netbanking.py` | Assisted net banking: Playwright drives the user's Edge; recorder JS + replay. Recorded steps are stored per `route_profile`: the bank (`HDFC`) or its cards (`HDFC:card`). Logs out before every close (`_logout`: learned hint → link/button named Log out → title/alt/id/href attributes → text; then the confirm pop-up; confirmed by "logged out" text or the login form). Logout clicks are never recorded as steps; the one you click yourself is remembered (`netbanking_logout:<bank>`). |
| `tracker/fetch/gmail.py`, `secrets.py` | Gmail (read-only) statement fetch; PDF passwords in Windows Credential Manager via `keyring`. |
| `tracker/logs.py` | Rotating logs in `data/logs/` (`app.log`, `mcp.log`). |
| `static/app.js`, `templates/index.html` | Single-page UI, no build step, no framework. Phones (≤760px, `style.css`) get the bottom menu, payment cards and the category picker sheet (`openPicker`); the `<select>` stays for computers. |
| `static/manifest.webmanifest`, `static/sw.js`, `static/icons/` | Home-screen app. `app.py` serves the manifest and `/sw.js` at the site root. The service worker caches only `/offline.html` and an icon, never `/api` data. It receives shared statements (POST `/share`) into the `expenses-shared` cache, and `receiveSharedFiles()` uploads and deletes them. |
| `scripts/hooks/` | Claude Code hooks (see below). |
| `tests/` | pytest. `helpers.py` builds synthetic statements, `fake_bank.py` is a fake net banking site, `conftest.py` has `FakeClassifier`. |

## How categorization works (keep this order)

`manual` (one-off) → user rules (exact merchant key, then contains/regex, longest first) → seed keyword rules (config order) → cached Claude answer (`source='llm'` exact rule) → Claude for the remaining *unique* merchant keys (results cached) → offline guess (`category_source='local'`, never cached) → `fallback` (Uncategorized).

- Transfer detection (`category_source='transfer'`) runs after categorization. It outranks everything except the user's rules.
- On card statements, payments received are marked Credit Card Payment *before* categorization, so they never go to Claude.
- Card purchases are spending; the bill payment is a transfer on both sides, so nothing counts twice. A bank-side bill payment with no imported card statement is shown as "not itemised" (spending is understated).
- A correction with scope `merchant` upserts a user exact rule and re-categorizes every non-manual transaction with that merchant key. Scope `single` marks one row `manual`.
- Claude picks the most specific sub-category; reports roll sub-categories up into their parent (`db.category_tree`).

## Conventions

- Money is stored as integer **paise**. Convert to rupees only at the edges (`service.rupees`, `insights.rupees`).
- Dates are ISO `yyyy-mm-dd` strings in SQLite.
- Plain `sqlite3` through `db.session()`, which commits on success. No ORM. Use parameterised SQL only.
- Python 3.12, ruff (`ruff.toml`, 120 columns). Logging via `logging.getLogger(__name__)`, never `print` in library code.
- API tests sign in first: `helpers.signed_in_client(app_module)` (local client, owner PIN `helpers.OWNER_PIN`). `tests/test_profiles.py` covers isolation between people.
- New behaviour gets a test. Tests use synthetic data only: build statements with `tests/helpers.py`, fake Claude with `FakeClassifier`. A unit test never calls the real API or CLI (`conftest.py` patches `default_classifier`).
- Card statement tests build files with `helpers.card_csv` or feed `parse_card_text` made-up text in both HDFC layouts (`tests/test_cards.py`).
- Net banking tests run against `tests/fake_bank.py` in headless Edge. Keep the fake bank as awkward as real banks: auto-numbered ids, scripted downloads, calendar pop-ups.

## Database changes

- Bump `SCHEMA_VERSION` and add a step in `_migrate()` / `_seed()`. The migration must keep existing data (`tests/test_db.py` shows the pattern).
- One-time data changes go behind an `app_settings` marker (e.g. `seeded:rules`), never on every start.
- Changing `config.SEED_RULES`: bump `db.SEED_RULES_VERSION`. On the next start the seed rules are replaced and re-run (no Claude) on every transaction they had categorized.
- Seed keywords match whole words anywhere in the narration, so avoid short ones that occur in remarks (a bare `CRED` caught "PAID VIA CRED" purchases).
- Before running anything that rewrites the user's real `data/expenses.db` (re-import, refine, migration), copy it to `data/expenses-backup-<timestamp>.db` first.

## Privacy rules (non-negotiable)

- **Categorization** sends Claude only: payee name, UPI ID with digits masked (`mask_upi_id`), channel, direction, and the narration with every number masked (`mask_for_llm`). Never amounts, balances, account or reference numbers.
- **Money plan** sends totals only. A payee name is included only if `advisor.is_business()` says it's clearly a business; anyone else becomes "a person (category)". `tests/test_insights.py` checks this.
- **Logs** (`data/logs/*.log`) record ids, counts, statuses and timings. Never amounts, narrations, payee or people's names, query strings, passwords or keys. `tests/test_logging.py` checks this; add a check when you add log lines.
- **People**: every `/api` route except `/api/auth/*` needs a session, and runs only against that person's folder. Never read paths from `config.DATA_DIR`/`INBOX_DIR` in request code: use `profiles.data_dir()`/`inbox_dir()`. Background threads must capture `profiles.current()` and run inside `profiles.use(...)`. The Claude subscription (CLI route) is for the owner's data only (`llm.subscription_allowed()`). First-time set-up only from a direct local request.
- **Net banking**: the user logs in themselves. Never record, store or fill login, password, OTP, PIN, card or phone fields. Never add anti-bot or captcha workarounds. MPIN / mobile-app access is out of scope.
- **The user's real data** (`data/`, statements, the database): read it only when the task needs it. Prefer aggregates, and mask digits and names in anything printed. Real data never goes into `tests/`, commits or Claude prompts beyond the rules above.
- Never commit `.env`, `data/`, `credentials.json`, `.claude/logs/` or `.claude/settings.local.json`.

## Claude usage in the app

- Categorizer: `claude-haiku-5-5`, `output_config.effort = "low"`, JSON-schema structured output, `max_tokens` 8000 (thinking counts toward it). No `temperature` (it returns a 400 on this model).
- Money plan: `config.PLAN_MODEL` (`claude-opus-5-5`) via `structured_completion`. The API path sends `fallbacks="default"` with the server-side-fallback beta.
- Route order is set in Settings (`app_settings.categorizer`: `auto` | `api` | `claude_code` | `local`). The CLI route runs `claude -p --json-schema ... --tools "" --strict-mcp-config --setting-sources "" --no-session-persistence`, with `ANTHROPIC_API_KEY` and `CLAUDECODE` removed from its environment, so it uses the user's own subscription sign-in. That's fine for this personal tool; it must not be used to serve other people.
- Read content blocks by `type`; handle `refusal` and `max_tokens` stop reasons.

## Windows notes

- Multi-line git commit messages: write them to a file and run `git commit -F file`. PowerShell breaks `-m` with here-strings.
- In the Bash tool, `echo`/`printf` collapse `\\` in JSON test payloads; use forward slashes in paths there.
- The venv is `.venv\Scripts\python.exe`. Playwright uses the installed Edge (`channel="msedge"`), so no browser download is needed.

## Hooks, skills and logs

- `.claude/settings.json` registers two async hooks:
  - `scripts/hooks/post_push_cleanup.py` (PostToolUse on Bash/PowerShell, acts only after a successful `git push`). Removes caches and unreferenced upload leftovers, and stops headless test browsers. Writes to `.claude/logs/cleanup.log`.
  - `scripts/hooks/audit_log.py` (PostToolUse + PostToolUseFailure, all tools). Writes one redacted line per tool call to `.claude/logs/claude-actions.jsonl`.
- Hook scripts must always exit 0 and never print secrets.
- Skill `commit-push` (`.claude/skills/commit-push/SKILL.md`): tests and lint, privacy check of the staged files, commit in the project style, push to `master`, verify. Use it when the user asks to commit and push.
- App logs: `data/logs/app.log` (web app) and `data/logs/mcp.log`. Check them first when the user reports an error.
