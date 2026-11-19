"""Expense tracker web app (FastAPI). Run: python app.py  ->  http://127.0.0.1:8765"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

import logging
import re
import time
import uuid

from fastapi import Body, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import config
from tracker import advisor, db, insights, logs, service
from tracker.fetch import gmail, netbanking, secrets
from tracker.importer import NeedsAccount
from tracker.parsers import SUPPORTED, PasswordRequired, StatementError

LOG_FILE = logs.setup("app")
log = logging.getLogger("app")
access = logging.getLogger("app.requests")

db.init_db()
app = FastAPI(title="Expense Tracker", docs_url="/api/docs", redoc_url=None)
app.mount("/static", StaticFiles(directory=config.BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=config.BASE_DIR / "templates")
log.info("Started: database %s, log %s", db.get_db_path().name, LOG_FILE)

# Polled every couple of seconds while a net banking window is open: keep it out of the log.
_QUIET_PATHS = {"/api/netbanking/session"}


@app.middleware("http")
async def _log_requests(request: Request, call_next):
    """One line per request: method, path, status and time. Never the query string (it can hold search text)."""
    started = time.perf_counter()
    path = request.url.path
    try:
        response = await call_next(request)
    except Exception:
        access.error("%s %s -> failed after %.0f ms", request.method, path, (time.perf_counter() - started) * 1000)
        raise
    if path.startswith("/static") or (path in _QUIET_PATHS and request.method == "GET"):
        return response
    ms = (time.perf_counter() - started) * 1000
    level = logging.ERROR if response.status_code >= 500 else logging.WARNING if response.status_code >= 400 else logging.INFO
    access.log(level, "%s %s -> %d (%.0f ms)", request.method, path, response.status_code, ms)
    return response


# ── Error mapping ─────────────────────────────────────────────────────────────


@app.exception_handler(service.NotFound)
async def _not_found(_: Request, exc: service.NotFound):
    return JSONResponse({"error": str(exc)}, status_code=404)


@app.exception_handler(ValueError)
async def _bad_request(_: Request, exc: ValueError):
    return JSONResponse({"error": str(exc)}, status_code=400)


@app.exception_handler(Exception)
async def _unexpected(request: Request, exc: Exception):
    log.exception("Unhandled error in %s %s", request.method, request.url.path)
    return JSONResponse({"error": f"Something went wrong ({type(exc).__name__}). Details are in data/logs/app.log."}, status_code=500)


# ── Page ──────────────────────────────────────────────────────────────────────


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(status_code=204)  # the page has an inline icon; keeps 404s out of the log


@app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
def index(request: Request):
    # Version stamp so the browser fetches app.js/style.css again whenever they change.
    static = config.BASE_DIR / "static"
    version = int(max((static / f).stat().st_mtime for f in ("app.js", "style.css")))
    return templates.TemplateResponse(request, "index.html", {"model": config.MODEL_ID, "v": version})


@app.get("/api/meta")
def meta():
    return {
        "categories": service.list_categories(include_inactive=True),
        "accounts": service.list_accounts(),
        "months": service.available_months(),
        "model": config.MODEL_ID,
        "claude_key_set": bool(os.getenv("ANTHROPIC_API_KEY")),
        "categorizer": service.categorizer_settings(),
    }


@app.get("/api/dashboard")
def dashboard(month: str | None = None, account_id: int | None = None):
    return service.dashboard(month, account_id)


@app.get("/api/overview")
def overview(period: str = "month", month: str | None = None, account_id: int | None = None):
    return insights.overview(period, month, account_id)


# ── Money plan & budgets ─────────────────────────────────────────────────────


@app.get("/api/plan")
def get_plan():
    return advisor.latest_plan() or {}


@app.post("/api/plan")
def create_plan(body: dict = Body(default={})):
    return advisor.create_plan(use_claude=bool(body.get("use_claude", True)))


@app.post("/api/plan/{plan_id}/apply-budgets")
def apply_plan_budgets(plan_id: int):
    return {"applied": advisor.apply_budgets(plan_id)}


@app.get("/api/budgets")
def budgets():
    return service.list_budgets()


@app.put("/api/budgets/{category_id}")
def set_budget(category_id: int, body: dict = Body(...)):
    value = body.get("monthly")
    return service.set_budget(category_id, float(value) if value not in (None, "") else None)


# ── Transactions ──────────────────────────────────────────────────────────────


@app.get("/api/transactions")
def transactions(
    q: str | None = None, month: str | None = None, account_id: int | None = None, category_id: int | None = None,
    source: str | None = None, direction: str | None = None, needs_review: bool = False, merchant_key: str | None = None,
    sort: str = "date_desc", limit: int = 100, offset: int = 0,
):
    return service.list_transactions(
        limit=min(limit, 1000), offset=offset, sort=sort, q=q, month=month, account_id=account_id,
        category_id=category_id, source=source, direction=direction, needs_review=needs_review, merchant_key=merchant_key,
    )


@app.get("/api/transactions/{txn_id}")
def transaction(txn_id: int):
    return service.get_transaction(txn_id)


@app.patch("/api/transactions/{txn_id}")
def update_transaction(txn_id: int, body: dict = Body(...)):
    out = {}
    if "note" in body:
        out["note"] = service.set_note(txn_id, body.get("note"))
    if body.get("category_id") is not None:
        out["category"] = service.set_category(txn_id, int(body["category_id"]), body.get("scope", "merchant"))
    return out


@app.post("/api/transactions/accept")
def accept(body: dict = Body(...)):
    return service.accept_categories([int(i) for i in body.get("ids", [])])


@app.post("/api/recategorize")
def recategorize(body: dict = Body(default={})):
    return service.recategorize(body.get("scope", "uncategorized"))


# ── Import ────────────────────────────────────────────────────────────────────


@app.post("/api/import")
def import_statement(
    file: UploadFile = File(...),
    account_id: int | None = Form(None),
    password: str | None = Form(None),
    remember_password: bool = Form(False),
):
    name = os.path.basename(file.filename or "statement")
    if os.path.splitext(name)[1].lower() not in SUPPORTED:
        return JSONResponse({"error": "Unsupported file type. Use .xls, .xlsx, .csv or .pdf"}, status_code=400)
    upload_dir = config.INBOX_DIR / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    path = upload_dir / f"{uuid.uuid4().hex[:8]}_{re.sub(r'[^A-Za-z0-9._-]', '_', name)}"
    path.write_bytes(file.file.read())

    candidates: list[str | None] = [password] if password else []
    if not password:
        ids = [account_id] if account_id else [a["id"] for a in service.list_accounts()]
        candidates += [p for p in (secrets.get_pdf_password(i) for i in ids) if p]
        candidates.append(None)
    keep = False
    try:
        last_exc: Exception | None = None
        for pw in candidates:
            try:
                result = service.import_file(path, account_id, pw, name)
                break
            except PasswordRequired as exc:
                last_exc = exc
        else:
            log.warning("Upload (%s) needs a PDF password (%s)", path.suffix, "wrong one given" if password else "none given")
            return JSONResponse(
                {"error": str(last_exc), "needs_password": True, "wrong_password": bool(password)}, status_code=409
            )
        if password and remember_password and result.account_id:
            secrets.set_pdf_password(result.account_id, password)
            log.info("Saved a PDF password for account %s in the credential store", result.account_id)
        keep = not result.duplicate_file
        return result.as_dict()
    except NeedsAccount as exc:
        log.warning("Upload (%s) needs an account choice: %s", path.suffix, type(exc).__name__)
        return JSONResponse({"error": str(exc), "needs_account": True, "bank": exc.bank, "last4": exc.last4}, status_code=409)
    except StatementError as exc:
        log.warning("Upload (%s) could not be read: %s", path.suffix, exc)
        return JSONResponse({"error": str(exc)}, status_code=422)
    finally:
        if not keep:
            path.unlink(missing_ok=True)


@app.get("/api/statements")
def statements():
    return service.list_statements()


@app.delete("/api/statements/{statement_id}")
def delete_statement(statement_id: int):
    return service.delete_statement(statement_id)


# ── Rules ─────────────────────────────────────────────────────────────────────


@app.get("/api/rules")
def rules(source: str | None = None, q: str | None = None):
    return service.list_rules(source, q)


@app.post("/api/rules")
def create_rule(body: dict = Body(...)):
    return service.create_rule(
        body.get("pattern", ""), int(body["category_id"]), body.get("match_type", "contains"), body.get("direction", "any")
    )


@app.patch("/api/rules/{rule_id}")
def update_rule(rule_id: int, body: dict = Body(...)):
    cat = body.get("category_id")
    return service.update_rule(
        rule_id, int(cat) if cat is not None else None, body.get("pattern"), body.get("match_type"), body.get("direction")
    )


@app.delete("/api/rules/{rule_id}")
def delete_rule(rule_id: int):
    return service.delete_rule(rule_id)


# ── Categories ────────────────────────────────────────────────────────────────


@app.get("/api/categories")
def categories():
    return service.list_categories(include_inactive=True)


@app.post("/api/categories")
def create_category(body: dict = Body(...)):
    parent = body.get("parent_id")
    return service.create_category(body.get("name", ""), body.get("kind", "expense"), None, body.get("description"),
                                   int(parent) if parent else None, body.get("grp") or None)


@app.patch("/api/categories/{category_id}")
def update_category(category_id: int, body: dict = Body(...)):
    return service.update_category(category_id, **body)


@app.post("/api/categories/{category_id}/merge")
def merge_category(category_id: int, body: dict = Body(...)):
    return service.merge_categories(category_id, int(body["target_id"]))


# ── Accounts ──────────────────────────────────────────────────────────────────


@app.get("/api/accounts")
def accounts():
    out = service.list_accounts()
    for a in out:
        a["has_pdf_password"] = secrets.has_pdf_password(a["id"])
    return out


@app.post("/api/accounts")
def create_account(body: dict = Body(...)):
    return service.create_account(**body)


@app.patch("/api/accounts/{account_id}")
def update_account(account_id: int, body: dict = Body(...)):
    return service.update_account(account_id, **body)


@app.delete("/api/accounts/{account_id}")
def delete_account(account_id: int):
    secrets.delete_pdf_password(account_id)
    return service.delete_account(account_id)


@app.put("/api/accounts/{account_id}/pdf-password")
def set_pdf_password(account_id: int, body: dict = Body(...)):
    pw = (body.get("password") or "").strip()
    if not pw:
        secrets.delete_pdf_password(account_id)
        return {"saved": False}
    secrets.set_pdf_password(account_id, pw)
    return {"saved": True}


# ── Gmail & Claude usage ─────────────────────────────────────────────────────


@app.get("/api/gmail/status")
def gmail_status():
    return gmail.status()


@app.post("/api/gmail/connect")
def gmail_connect():
    try:
        return gmail.connect()
    except FileNotFoundError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.post("/api/gmail/disconnect")
def gmail_disconnect():
    gmail.disconnect()
    return gmail.status()


@app.post("/api/gmail/sync")
def gmail_sync(body: dict = Body(default={})):
    try:
        return gmail.sync(body.get("account_id"))
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


# ── Assisted net banking ─────────────────────────────────────────────────────


@app.get("/api/netbanking/banks")
def netbanking_banks():
    return netbanking.banks_info()


@app.put("/api/netbanking/banks/{bank}")
def netbanking_set_bank(bank: str, body: dict = Body(...)):
    if bank not in netbanking.BANKS:
        raise service.NotFound(f"Unknown bank {bank}")
    netbanking.set_login_url(bank, body.get("login_url", ""))
    return netbanking.banks_info()


@app.delete("/api/netbanking/banks/{bank}/route")
def netbanking_forget_route(bank: str):
    netbanking.forget_route(bank)
    return netbanking.banks_info()


@app.post("/api/netbanking/session")
def netbanking_start(body: dict = Body(...)):
    return netbanking.start_session(int(body["account_id"]))


@app.get("/api/netbanking/session")
def netbanking_status():
    return netbanking.session_status() or {"state": "none"}


@app.post("/api/netbanking/session/{action}")
def netbanking_action(action: str, body: dict = Body(default={})):
    if action == "replay":
        return netbanking.session_command("replay", start=body.get("start"), end=body.get("end"))
    if action == "record_stop":
        return netbanking.session_command("record_stop", save=bool(body.get("save", True)))
    if action in ("record_start", "close"):
        return netbanking.session_command(action)
    raise service.NotFound(f"Unknown action {action}")


@app.get("/api/settings/categorizer")
def get_categorizer(check: bool = False):
    return service.categorizer_settings(check_cli_login=check)


@app.put("/api/settings/categorizer")
def put_categorizer(body: dict = Body(...)):
    return service.set_categorizer_mode(body.get("mode", "auto"))


@app.get("/api/llm/usage")
def llm_usage():
    return service.llm_usage()


if __name__ == "__main__":
    import uvicorn

    # log_config=None: keep our logging setup instead of uvicorn's own (requests are logged by the middleware).
    uvicorn.run(app, host=config.HOST, port=config.PORT, log_config=None)
