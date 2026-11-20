"""Assisted net banking: you log in yourself, the app fetches and imports the statement.

How it works:
  1. The app opens a real browser window (your installed Edge or Chrome, fresh profile,
     nothing saved afterwards) at your bank's login page.
  2. You log in yourself: user ID, password, OTP, captcha. The app never reads or stores
     what you type there.
  3. Any statement you download in that window is captured and imported into the account.
  4. Optionally, once you're logged in, "Record my steps" remembers the clicks you make to
     reach the statement (menu, account, period, format, download). Next time the app
     replays them for you after you log in, with the date range filled in.

The recorder only keeps clicks, dropdown choices and date fields. It ignores password,
OTP, PIN, user-ID and card fields entirely, and recording only starts after you've
logged in. No anti-bot or captcha workarounds: if a bank blocks automated browsers,
download the statement in your normal browser and use the Import tab.
"""

import json
import logging
import os
import queue
import re
import threading
import time
from concurrent.futures import Future
from datetime import date, datetime, timedelta
from pathlib import Path

from tracker import db, profiles, service
from tracker.fetch.secrets import get_pdf_password
from tracker.importer import NeedsAccount
from tracker.parsers import SUPPORTED, PasswordRequired, StatementError

log = logging.getLogger(__name__)

BANKS = {
    # Banks moved to RBI's .bank.in domains; checked Nov 2026 (the old netbanking.hdfcbank.com redirects
    # to now.hdfc.bank.in). CUB's older system is at www.onlinebanking.cub.bank.in. Editable in the app.
    "HDFC": {"name": "HDFC Bank NetBanking", "login_url": "https://now.hdfc.bank.in/"},
    "CUB": {"name": "CUB All in One", "login_url": "https://online.cub.bank.in/"},
    "OTHER": {"name": "Net banking", "login_url": ""},
}
IDLE_TIMEOUT = 20 * 60  # close the browser after 20 minutes without activity
STEP_TIMEOUT = 30.0
LOGOUT_TIMEOUT = 15.0  # how long to wait for the bank to confirm the logout

# Logging out before the browser closes: closing only throws away the cookies, while the bank keeps the
# session alive on its side until it times out.
_LOGOUT_RE = re.compile(r"\b(log\s*-?\s*(out|off)|sign\s*-?\s*(out|off))\b", re.IGNORECASE)
_LOGOUT_ATTR_CSS = ", ".join(
    f"[{attr}*='{word}' i]"
    for attr in ("title", "aria-label", "alt", "id", "name", "value", "href", "onclick")
    for word in ("logout", "log out", "log-out", "logoff", "log off", "signout", "sign out", "sign-out")
)
_CONFIRM_RE = re.compile(r"^\s*(yes|ok|okay|confirm|continue|log\s*-?\s*(out|off)|sign\s*-?\s*(out|off))\b", re.IGNORECASE)
_DIALOG_CSS = ("[role=dialog], [role=alertdialog], .modal, [class*='modal' i], [class*='popup' i], [class*='dialog' i], "
               "[id*='modal' i], [id*='popup' i], [id*='dialog' i]")
_LOGGED_OUT_RE = re.compile(
    r"(logged|signed)\s*(out|off)|successfully\s+log|thank\s+you\s+for\s+(banking|using|visiting)|"
    r"session\s+(has\s+)?(ended|expired|timed\s*out)", re.IGNORECASE,
)
_STATEMENT_CONTENT_TYPES = ("application/pdf", "application/vnd.ms-excel", "spreadsheetml", "text/csv")

RECORDER_JS = r"""
(() => {
  if (window.__etRecorder) return;
  window.__etRecorder = true;
  const SENSITIVE = /pass|pwd|otp|pin|cvv|captcha|secret|answer|user.?id|userid|login|cust(omer)?.?id|card|mobile|phone|email/i;
  const DATE_RE = /^\s*(\d{1,2}[\/\-. ](\d{1,2}|[A-Za-z]{3})[\/\-. ]\d{2,4}|\d{4}-\d{2}-\d{2})\s*$/;
  const esc = (s) => (window.CSS && CSS.escape) ? CSS.escape(s) : s;
  const unique = (sel) => { try { return document.querySelectorAll(sel).length === 1; } catch (e) { return false; } };
  // Auto-numbered ids (bb_input_7, mat-select-12) change between visits: never rely on them.
  const stable = (v) => v && v.length < 60 && !/\d{3,}/.test(v) && !/(^|[_\-:])\d+$/.test(v);
  function fieldLabel(el) {
    let t = '';
    if (el.labels && el.labels.length) t = el.labels[0].innerText;
    if (!t && el.getAttribute('aria-labelledby')) {
      t = el.getAttribute('aria-labelledby').split(/\s+/).map((id) => (document.getElementById(id) || {}).innerText || '').join(' ');
    }
    t = (t || el.getAttribute('aria-label') || el.placeholder || '').trim().replace(/\s+/g, ' ').replace(/\s*\*$/, '');
    return t && t.length <= 60 && !/\d{5,}/.test(t) ? t : '';
  }
  const send = (p) => { try { if (window.__etEvent) window.__etEvent(Object.assign({ frame: window.name || '', path: location.pathname }, p)); } catch (e) {} };
  const isSensitive = (el) => el.type === 'password' ||
    SENSITIVE.test([el.name, el.id, el.placeholder, el.getAttribute('aria-label'), el.autocomplete].join(' '));
  function text(el) {
    const t = (el.innerText || el.value || el.getAttribute('aria-label') || el.title || el.alt || '').trim().replace(/\s+/g, ' ');
    return t && t.length <= 60 && !/\d{5,}/.test(t) ? t : '';
  }
  function cssPath(el) {
    const parts = [];
    while (el && el.nodeType === 1 && parts.length < 8) {
      if (stable(el.id) && unique('#' + esc(el.id))) { parts.unshift('#' + esc(el.id)); break; }
      let p = el.tagName.toLowerCase();
      const parent = el.parentElement;
      if (parent) {
        const same = Array.from(parent.children).filter((c) => c.tagName === el.tagName);
        if (same.length > 1) p += ':nth-of-type(' + (same.indexOf(el) + 1) + ')';
      }
      parts.unshift(p);
      el = parent;
    }
    return parts.join(' > ');
  }
  function describe(el) {
    const tag = el.tagName.toLowerCase();
    const c = [];
    if (stable(el.id) && unique('#' + esc(el.id))) c.push('#' + esc(el.id));
    const name = el.getAttribute('name');
    if (stable(name) && unique(tag + '[name="' + name + '"]')) c.push(tag + '[name="' + name + '"]');
    const t = (tag === 'select' || (tag === 'input' && !['submit', 'button'].includes(el.type))) ? '' : text(el);
    if (t) c.push(tag + ':text-is(' + JSON.stringify(t) + ')');
    c.push(cssPath(el));
    const fl = ['input', 'select', 'textarea'].includes(tag) ? fieldLabel(el) : '';
    return { tag: tag, candidates: c, field_label: fl, label: t || fl || (stable(name) ? name : '') || (stable(el.id) ? el.id : '') || tag };
  }
  const reported = new WeakMap();
  function reportDates() {
    // Date pickers often set values by script without a change event; catch them before each click.
    document.querySelectorAll('input').forEach((el) => {
      if (isSensitive(el) || !['text', 'date', ''].includes(el.type)) return;
      const v = (el.value || '').trim();
      if (!DATE_RE.test(v) || reported.get(el) === v) return;
      const r = el.getBoundingClientRect();
      if (!r.width && !r.height) return;
      reported.set(el, v);
      send(Object.assign({ action: 'date', value: v, input_type: el.type }, describe(el)));
    });
  }
  const PICKER = '[class*="datepicker" i],[class*="calendar" i],[class*="picker" i],[id*="calendar" i],[id*="datepicker" i]';
  // A click on the bank's Log out control: never part of a recording, but remembered so the app can log out for you.
  const LOGOUT = /log\s*-?\s*(out|off)|sign\s*-?\s*(out|off)/i;
  function isLogout(el) {
    for (let n = el, i = 0; n && n.nodeType === 1 && i < 4; n = n.parentElement, i++) {
      const t = (n.innerText || '').length <= 40 ? n.innerText : '';
      const attrs = ['title', 'aria-label', 'alt', 'id', 'href', 'onclick', 'value'].map((a) => n.getAttribute(a) || '');
      if (LOGOUT.test([t].concat(attrs).join(' '))) return true;
    }
    return false;
  }
  document.addEventListener('click', (e) => {
    // Only your own clicks: pages also click hidden links by script to start a download.
    if (!e.isTrusted) return;
    // Clicks inside a calendar pop-up pick a specific day; the date field's value is recorded instead.
    if (e.target.closest && e.target.closest(PICKER) && e.target.tagName !== 'INPUT') { setTimeout(reportDates, 0); return; }
    const el = e.target.closest('a,button,input[type=submit],input[type=button],input[type=image],input[type=radio],input[type=checkbox],[role=button],[role=tab],[role=menuitem],[role=option],[onclick],li,td,label,span,img') || e.target;
    if (!el || el === document.body || el === document.documentElement) return;
    if (['SELECT', 'OPTION', 'TEXTAREA'].includes(el.tagName)) return;
    if (el.tagName === 'INPUT' && !['submit', 'button', 'image', 'radio', 'checkbox'].includes(el.type)) return;
    if (isLogout(e.target)) { send(Object.assign({ action: 'logout' }, describe(el))); return; }
    reportDates();
    send(Object.assign({ action: 'click' }, describe(el)));
  }, true);
  document.addEventListener('change', (e) => {
    const el = e.target;
    if (!el || !el.tagName) return;
    if (el.tagName === 'SELECT') {
      const opt = el.options[el.selectedIndex];
      send(Object.assign({ action: 'select', value: el.value, option_text: opt ? opt.text.trim() : '' }, describe(el)));
    } else if (el.tagName === 'INPUT' && !isSensitive(el)) {
      reportDates();
    }
  }, true);
})();
"""


# ── Routes (recorded steps) ───────────────────────────────────────────────────


def route_profile(bank: str, card: bool = False) -> str:
    """Recorded steps are kept per bank, and separately for that bank's credit cards (a different statement page)."""
    return f"{bank}:card" if card else bank


def _route_key(profile: str) -> str:
    return f"netbanking_route:{profile}"


def get_route(profile: str) -> dict | None:
    with db.session() as conn:
        raw = db.get_setting(conn, _route_key(profile))
    if not raw:
        return None
    route = json.loads(raw)
    # Routes recorded by earlier versions: detect month pickers, drop script-made download-link clicks.
    steps = []
    for s in route["steps"]:
        if s["action"] == "click" and s.get("candidates") in (["html > body > a"], ["body > a"]):
            continue
        if s["action"] == "select" and "month_format" not in s:
            s["month_format"] = month_option_format(s.get("option_text") or "")
        steps.append(s)
    route["steps"] = steps
    return route


def save_route(profile: str, steps: list[dict]) -> dict:
    route = {"steps": steps, "recorded_at": datetime.now().isoformat(timespec="seconds")}
    with db.session() as conn:
        db.set_setting(conn, _route_key(profile), json.dumps(route))
    return route


def forget_route(profile: str) -> None:
    with db.session() as conn:
        conn.execute("DELETE FROM app_settings WHERE key = ?", (_route_key(profile),))


def get_logout_hint(bank: str) -> list[str]:
    """Selectors of the Log out control you clicked at this bank before, tried first when logging out for you."""
    with db.session() as conn:
        raw = db.get_setting(conn, f"netbanking_logout:{bank}")
    return json.loads(raw) if raw else []


def save_logout_hint(bank: str, candidates: list[str]) -> None:
    with db.session() as conn:
        db.set_setting(conn, f"netbanking_logout:{bank}", json.dumps(candidates[:4]))


def login_url(bank: str) -> str:
    with db.session() as conn:
        return db.get_setting(conn, f"netbanking_login_url:{bank}", BANKS.get(bank, BANKS["OTHER"])["login_url"]) or ""


def set_login_url(bank: str, url: str) -> None:
    url = url.strip()
    if url and not re.match(r"^https://", url):
        raise ValueError("The login page must be an https:// address")
    with db.session() as conn:
        db.set_setting(conn, f"netbanking_login_url:{bank}", url)


def banks_info() -> list[dict]:
    out = []
    def describe(route):
        return {"recorded_at": route["recorded_at"], "steps": [describe_step(s) for s in route["steps"]]} if route else None

    for bank, info in BANKS.items():
        out.append({
            "bank": bank,
            "name": info["name"],
            "login_url": login_url(bank),
            "route": describe(get_route(route_profile(bank))),
            "card_route": describe(get_route(route_profile(bank, card=True))),
        })
    return out


def describe_step(step: dict) -> str:
    label = step.get("label") or step.get("tag") or "element"
    if step["action"] == "click":
        return f"Click “{label}”"
    if step["action"] == "select" and step.get("month_format"):
        return f"Choose the period's month in {label}"
    if step["action"] == "select":
        return f"Choose “{step.get('option_text') or step.get('value')}” in {label}"
    if step["action"] == "date":
        return f"Enter the {'start' if step.get('role') == 'from' else 'end'} date in {label}"
    return step["action"]


# ── Date handling ─────────────────────────────────────────────────────────────

_DATE_PATTERNS = [
    (re.compile(r"^\d{4}-\d{2}-\d{2}$"), "%Y-%m-%d"),
    (re.compile(r"^\d{1,2}([/\-. ])\d{1,2}\1\d{4}$"), "%d{sep}%m{sep}%Y"),
    (re.compile(r"^\d{1,2}([/\-. ])\d{1,2}\1\d{2}$"), "%d{sep}%m{sep}%y"),
    (re.compile(r"^\d{1,2}([/\-. ])[A-Za-z]{3}\1\d{4}$"), "%d{sep}%b{sep}%Y"),
    (re.compile(r"^\d{1,2}([/\-. ])[A-Za-z]{3}\1\d{2}$"), "%d{sep}%b{sep}%y"),
]


def date_format(value: str) -> tuple[str, bool] | None:
    """'01/10/2026' -> ('%d/%m/%Y', False); '01-OCT-2026' -> ('%d-%b-%Y', True)."""
    v = value.strip()
    for pattern, fmt in _DATE_PATTERNS:
        m = pattern.match(v)
        if m:
            sep = m.group(1) if m.groups() else ""
            return fmt.format(sep=sep), v.isupper() and any(ch.isalpha() for ch in v)
    return None


_MONTH_OPTION = re.compile(r"^([A-Za-z]{3,9})([\s\-/,']+)(\d{4}|\d{2})$")


def month_option_format(text: str) -> str | None:
    """'November 2026' -> '%B %Y', 'Nov-26' -> '%b-%y'; None if the option isn't a month."""
    m = _MONTH_OPTION.match(text.strip())
    if not m:
        return None
    word, sep, year = m.groups()
    for fmt in ("%B", "%b"):
        try:
            datetime.strptime(word.title(), fmt)
        except ValueError:
            continue
        return f"{fmt}{sep}{'%Y' if len(year) == 4 else '%y'}"
    return None


def format_date(d: date, fmt: str, upper: bool = False) -> str:
    s = d.strftime(fmt)
    return s.upper() if upper else s


def finalize_steps(raw: list[dict]) -> list[dict]:
    """Collapse the recording into replayable steps; label date fields as start/end of the period."""
    steps: list[dict] = []
    for ev in raw:
        if ev["action"] == "date":
            fmt = date_format(ev.get("value", ""))
            if not fmt:
                continue
            ev = {**ev, "format": fmt[0], "upper": fmt[1]}
            # A later report for the same field replaces the earlier one.
            steps = [s for s in steps if not (s["action"] == "date" and s["candidates"] == ev["candidates"] and s["frame"] == ev["frame"])]
        elif steps and ev["action"] == "select" and steps[-1]["action"] == "select" and steps[-1]["candidates"] == ev["candidates"]:
            steps.pop()  # changed the same dropdown twice: keep the final choice
        elif steps and ev["action"] == "click" and steps[-1].get("candidates") == ev["candidates"]:
            continue  # double click
        steps.append({k: ev.get(k) for k in ("action", "tag", "candidates", "label", "field_label", "frame", "path", "value", "option_text", "format", "upper", "input_type")})
    # A field label only helps find a field if no other recorded field shares it.
    labels = [s.get("field_label") for s in steps if s.get("field_label")]
    for s in steps:
        if s.get("field_label") and labels.count(s["field_label"]) > 1:
            s["field_label"] = None
        if s["action"] == "select":
            s["month_format"] = month_option_format(s.get("option_text") or "")
    dates = [s for s in steps if s["action"] == "date"]
    parsed = []
    for s in dates:
        try:
            parsed.append((datetime.strptime(s["value"].strip().title() if "%b" in s["format"] else s["value"].strip(), s["format"]), s))
        except ValueError:
            parsed.append((datetime.max, s))
    parsed.sort(key=lambda p: p[0])
    for i, (_, s) in enumerate(parsed):
        s["role"] = "from" if i == 0 else "to"
        s.pop("value", None)  # the recorded date itself isn't needed for replay
    return steps


def default_period(account_id: int) -> tuple[date, date]:
    """From two days before the last transaction we have (overlap is deduplicated), to today."""
    with db.session() as conn:
        last = conn.execute("SELECT MAX(txn_date) FROM transactions WHERE account_id = ?", (account_id,)).fetchone()[0]
    end = date.today()
    start = date.fromisoformat(last) - timedelta(days=2) if last else end - timedelta(days=90)
    return min(start, end), end


# ── Browser session ───────────────────────────────────────────────────────────


class StepNotFound(Exception):
    pass


class NetBankingSession:
    def __init__(self, account: dict, url: str, headless: bool = False, channel: str | None = None):
        self.account = account
        self.profile_user = profiles.current()  # whose space the downloads go into
        self.bank = account["bank"] if account["bank"] in BANKS else "OTHER"
        self.profile = route_profile(self.bank, account.get("type") == "credit_card")
        self.url = url
        self.headless = headless
        self.channel = channel
        self.state = "starting"
        self.message = "Opening the browser…"
        self.imports: list[dict] = []
        self.recording = False
        self.raw_steps: list[dict] = []
        self.progress: str | None = None
        self.logout: str | None = None  # confirmed | clicked | already | not_found, once the app tried
        self.logout_failed = False  # couldn't find Log out: the window stays open until you close it anyway
        self.started_at = time.time()
        self._last_activity = time.time()
        self._commands: queue.Queue = queue.Queue()
        self._downloads: list = []
        self._inline_pdfs: list = []
        self._seen_urls: set[str] = set()
        self._thread = threading.Thread(target=self._run, name="netbanking", daemon=True)
        self._lock = threading.Lock()

    # Public API (called from web request threads)
    def start(self) -> "NetBankingSession":
        self._thread.start()
        return self

    def command(self, name: str, **kwargs) -> Future:
        fut: Future = Future()
        self._commands.put((name, kwargs, fut))
        return fut

    def call(self, fn) -> Future:
        """Run fn(page) on the browser thread (used by tests to play the user)."""
        return self.command("call", fn=fn)

    @property
    def active(self) -> bool:
        return self.state not in ("closed", "error")

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "account_id": self.account["id"],
                "account_name": self.account["name"],
                "bank": self.bank,
                "state": self.state,
                "message": self.message,
                "progress": self.progress,
                "recording": self.recording,
                "recorded_steps": [describe_step(s) for s in finalize_steps(list(self.raw_steps))] if self.recording else [],
                "imports": list(self.imports),
                "has_route": get_route(self.profile) is not None,
                "logout_failed": self.logout_failed,
            }

    # Browser thread
    def _set(self, state: str | None = None, message: str | None = None, progress: str | None = None):
        with self._lock:
            if state and state != self.state:
                log.log(logging.WARNING if state == "error" else logging.INFO, "Net banking %s (account %s): %s -> %s%s",
                        self.bank, self.account["id"], self.state, state, f" ({message})" if state == "error" else "")
            if state:
                self.state = state
            if message is not None:
                self.message = message
            self.progress = progress

    def _run(self) -> None:
        with profiles.use(self.profile_user):
            self._run_browser()

    def _run_browser(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self._set("error", "Playwright isn't installed. Run: .venv\\Scripts\\python.exe -m pip install playwright")
            return
        try:
            with sync_playwright() as pw:
                browser = self._launch(pw)
                context = browser.new_context(accept_downloads=True, viewport=None if not self.headless else {"width": 1280, "height": 900})
                context.expose_binding("__etEvent", self._on_event)
                context.add_init_script(RECORDER_JS)
                context.on("page", self._watch_page)
                page = context.new_page()
                self._watch_page(page)
                message = self._idle_message()
                if self.url:
                    try:
                        page.goto(self.url, wait_until="domcontentloaded", timeout=60_000)
                    except Exception as exc:
                        message = (f"Couldn't open {self.url} ({str(exc).splitlines()[0][:120]}). "
                                   "Type your bank's net banking address in the browser window, and fix it in the panel here.")
                self._set("open", message)
                self._loop(context)
                try:
                    browser.close()
                except Exception:
                    pass
        except Exception as exc:  # launch failures, bank page errors
            log.exception("Net banking session failed")
            self._set("error", f"Browser session ended: {str(exc).splitlines()[0][:200]}")
            return
        if self.state != "error":
            self._set("closed", self._closing_message())

    def _launch(self, pw):
        channels = [self.channel] if self.channel else ["msedge", "chrome", None]
        last = None
        for ch in channels:
            try:
                return pw.chromium.launch(channel=ch, headless=self.headless) if ch else pw.chromium.launch(headless=self.headless)
            except Exception as exc:
                last = exc
        raise RuntimeError(f"Couldn't start Edge or Chrome: {last}")

    def _watch_page(self, page) -> None:
        if getattr(page, "_et_watched", False):
            return  # context "page" event and our own call can both arrive for the first tab
        page._et_watched = True
        page.on("download", lambda d: self._downloads.append(d))
        page.on("response", self._on_response)

    def _on_response(self, response) -> None:
        # PDFs opened inline in the browser's viewer never fire a download event.
        try:
            ctype = (response.headers.get("content-type") or "").lower()
            disp = (response.headers.get("content-disposition") or "").lower()
        except Exception:
            return
        if "application/pdf" in ctype and "attachment" not in disp and response.ok and response.url not in self._seen_urls:
            self._seen_urls.add(response.url)
            self._inline_pdfs.append(response)

    def _on_event(self, source, payload) -> None:
        self._last_activity = time.time()
        if isinstance(payload, dict) and payload.get("action") == "logout":
            save_logout_hint(self.bank, payload.get("candidates") or [])  # you logged out yourself: remember where
            return
        if self.recording and isinstance(payload, dict) and payload.get("action") in ("click", "select", "date"):
            with self._lock:
                self.raw_steps.append(payload)

    def _idle_message(self) -> str:
        bank = BANKS[self.bank]["name"]
        if get_route(self.profile):
            return (f"Log in to {bank} in the browser window, then click “Fetch statement”. "
                    "You can also download statements yourself: they're imported automatically.")
        return (f"Log in to {bank} in the browser window. Then either download a statement yourself "
                "(it's imported automatically), or click “Record my steps” first so the app can do it for you next time.")

    def _closing_message(self) -> str:
        n = sum(1 for i in self.imports if i.get("ok"))
        bank = BANKS[self.bank]["name"]
        first = {
            "confirmed": f"Logged out of {bank} and closed the browser.",
            "clicked": f"Clicked Log out on {bank} and closed the browser.",
            "already": "You were logged out. Browser closed.",
            "not_found": f"Browser closed without logging out: {bank} ends the session itself after a few minutes.",
        }.get(self.logout or "", "Browser closed.")
        return f"{first} {n} statement{'s' if n != 1 else ''} imported." if n else f"{first} Nothing was downloaded."

    def _current_page(self, context):
        pages = [p for p in context.pages if not p.is_closed()]
        return pages[-1] if pages else None

    # Logging out
    def _ask_to_log_out(self, fetched: bool = False) -> None:
        self.logout_failed = True
        self._set("open", ("Statement fetched and imported. " if fetched else "")
                  + f"Couldn't find the Log out button on {BANKS[self.bank]['name']}. Log out in the browser window yourself "
                    "(the app remembers where you clicked for next time), then click “Close anyway”.")

    def _all_frames(self, context) -> list:
        pages = [p for p in context.pages if not p.is_closed()]
        return [f for p in reversed(pages) for f in p.frames]

    @staticmethod
    def _first_visible(loc, limit: int = 5):
        try:
            for i in range(min(loc.count(), limit)):
                el = loc.nth(i)
                if el.is_visible():
                    return el
        except Exception:
            pass  # a frame that navigated away meanwhile
        return None

    def _find_logout(self, context):
        hint = get_logout_hint(self.bank)
        for frame in self._all_frames(context):
            options = [frame.locator(c) for c in hint] + [
                frame.get_by_role("link", name=_LOGOUT_RE), frame.get_by_role("button", name=_LOGOUT_RE),
                frame.locator(_LOGOUT_ATTR_CSS), frame.get_by_text(_LOGOUT_RE),
            ]
            for loc in options:
                el = self._first_visible(loc)
                if el is not None:
                    return el
        return None

    def _find_confirm(self, context):
        """The "Yes" / "OK" / "Log out" button of an "Are you sure?" pop-up."""
        for frame in self._all_frames(context):
            box = frame.locator(_DIALOG_CSS)
            for loc in (box.get_by_role("button", name=_CONFIRM_RE), box.get_by_role("link", name=_CONFIRM_RE)):
                el = self._first_visible(loc)
                if el is not None:
                    return el
        return None

    def _password_visible(self, context) -> bool:
        return any(self._first_visible(f.locator("input[type=password]")) is not None for f in self._all_frames(context))

    def _says_logged_out(self, context) -> bool:
        return any(self._first_visible(f.get_by_text(_LOGGED_OUT_RE)) is not None for f in self._all_frames(context))

    def _logout(self, context) -> str:
        """Log out of the bank. Returns confirmed | clicked | already | not_found (or closed: no window left)."""
        if self._current_page(context) is None:
            return "closed"
        if self.logout in ("confirmed", "clicked", "already"):
            return self.logout
        self._set(message="Logging out of the bank…", progress=None)
        control = self._find_logout(context)
        if control is None:
            # A login page (never logged in, or you logged out yourself) has nothing to log out of.
            outcome = "already" if self._says_logged_out(context) or self._password_visible(context) else "not_found"
        else:
            outcome = self._click_logout(context, control)
        self.logout = outcome
        log.info("Net banking %s (account %s): logout %s", self.bank, self.account["id"], outcome)
        return outcome

    def _click_logout(self, context, control) -> str:
        pages = [p for p in context.pages if not p.is_closed()]

        def accept(dialog):  # a browser confirm() box: "Do you want to log out?"
            try:
                dialog.accept()
            except Exception:
                pass

        for p in pages:
            p.on("dialog", accept)
        password_before = self._password_visible(context)
        try:
            try:
                control.click(timeout=5_000)
            except Exception:
                control.evaluate("el => el.click()")
            confirmed_popup = False
            deadline = time.time() + LOGOUT_TIMEOUT
            while time.time() < deadline:
                page = self._current_page(context)
                if page is None:
                    return "confirmed"  # the bank closed its own window
                if self._says_logged_out(context) or (not password_before and self._password_visible(context)):
                    return "confirmed"
                if not confirmed_popup:
                    button = self._find_confirm(context)
                    if button is not None:
                        try:
                            button.click(timeout=3_000)
                            confirmed_popup = True
                        except Exception:
                            pass
                try:
                    page.wait_for_timeout(400)
                except Exception:
                    pass
            return "clicked"
        except Exception as exc:
            log.warning("Net banking %s: logout click failed: %s", self.bank, str(exc).splitlines()[0][:120])
            return "not_found"
        finally:
            for p in pages:
                try:
                    p.remove_listener("dialog", accept)
                except Exception:
                    pass

    def _loop(self, context) -> None:
        while True:
            page = self._current_page(context)
            if page is None:
                return  # you closed the window
            try:
                name, kwargs, fut = self._commands.get_nowait()
            except queue.Empty:
                name = None
            if name == "close":
                outcome = self._logout(context)
                if outcome == "not_found" and not kwargs.get("force"):
                    self._ask_to_log_out()
                    fut.set_result({"closed": False, "logout": outcome})
                    continue
                fut.set_result({"closed": True, "logout": outcome})
                return
            if name:
                self._last_activity = time.time()
                try:
                    result = self._handle(name, kwargs, context, page)
                    fut.set_result(result)
                except Exception as exc:
                    fut.set_exception(exc)
                    result = None
                if name == "replay" and isinstance(result, dict) and result.get("ok"):
                    # Fetched: log out and close, so the bank session doesn't stay open behind you.
                    if self._logout(context) != "not_found":
                        return
                    self._ask_to_log_out(fetched=True)
            self._process_downloads()
            if time.time() - self._last_activity > IDLE_TIMEOUT:
                self._logout(context)
                return
            try:
                page.wait_for_timeout(200)  # lets the browser deliver events (downloads, recorder)
            except Exception:
                if self._current_page(context) is None:
                    return

    def _handle(self, name: str, kwargs: dict, context, page):
        if name == "call":
            return kwargs["fn"](page)
        if name == "record_start":
            with self._lock:
                self.raw_steps = []
                self.recording = True
            self._set("recording", "Recording your clicks. Open the account statement, choose the period and format, and download it. "
                                   "The recording stops by itself when the statement downloads.")
            return True
        if name == "record_stop":
            return self._stop_recording(save=kwargs.get("save", True))
        if name == "replay":
            return self._replay(context, kwargs["start"], kwargs["end"])
        raise ValueError(f"Unknown command {name}")

    def _stop_recording(self, save: bool = True) -> dict | None:
        with self._lock:
            self.recording = False
            raw = list(self.raw_steps)
        steps = finalize_steps(raw)
        if save and steps:
            save_route(self.profile, steps)
            log.info("Net banking %s: saved a recording of %d steps (%s)", self.bank, len(steps), ", ".join(s["action"] for s in steps))
            self._set("open", f"Saved {len(steps)} steps for {BANKS[self.bank]['name']}. Next time: log in, then click “Fetch statement”.")
            return {"steps": [describe_step(s) for s in steps]}
        self._set("open", self._idle_message())
        return None

    # Replay
    def _replay(self, context, start: date, end: date) -> dict:
        route = get_route(self.profile)
        if not route:
            raise ValueError("No recorded steps for this bank yet. Use “Record my steps” once.")
        steps = route["steps"]
        self._set("replaying", f"Fetching the statement for {start:%d %b %Y} – {end:%d %b %Y}…")
        before = len(self.imports)
        for i, step in enumerate(steps, 1):
            if len(self.imports) > before:
                break  # the statement already arrived; later steps (if any) aren't needed
            self._set(progress=f"Step {i} of {len(steps)}: {describe_step(step)}")
            loc = self._find(context, step)
            if loc is None:
                if step["action"] == "select" and step.get("month_format"):
                    continue  # a month picker that no longer offers this option: the date fields set the period
                log.warning("Net banking %s replay: step %d of %d (%s) not found", self.bank, i, len(steps), step["action"])
                self._set("open", f"Couldn't find step {i} ({describe_step(step)}). The bank's page may have changed. "
                                  "Finish by hand: whatever you download is still imported. Record the steps again to fix it.")
                return {"ok": False, "failed_step": i}
            try:
                self._perform(loc, step, start, end)
            except Exception as exc:
                if step["action"] == "select" and step.get("month_format"):
                    continue
                log.warning("Net banking %s replay: step %d of %d (%s) failed: %s", self.bank, i, len(steps), step["action"],
                            str(exc).splitlines()[0][:160])
                self._set("open", f"Step {i} ({describe_step(step)}) didn't work: {str(exc).splitlines()[0][:120]}. "
                                  "Finish by hand: whatever you download is still imported.")
                return {"ok": False, "failed_step": i}
            self._settle(context)
        deadline = time.time() + 60
        while time.time() < deadline and len(self.imports) == before:
            self._process_downloads()
            page = self._current_page(context)
            if page is None:
                break
            page.wait_for_timeout(300)
        ok = len(self.imports) > before
        log.info("Net banking %s replay for %s..%s: %s", self.bank, start, end, "statement imported" if ok else "no download")
        self._set("open", "Statement fetched and imported." if ok else
                  "Steps done, but no statement downloaded yet. If the bank shows it on screen, click its download button yourself.")
        return {"ok": ok}

    def _frames(self, context, step: dict):
        pages = [p for p in context.pages if not p.is_closed()]
        frames = [f for p in reversed(pages) for f in p.frames]
        wanted = step.get("frame") or ""
        return sorted(frames, key=lambda f: 0 if (f.name or "") == wanted else 1)

    def _find(self, context, step: dict):
        deadline = time.time() + STEP_TIMEOUT
        while time.time() < deadline:
            for frame in self._frames(context, step):
                if step.get("field_label"):  # the visible label survives re-renders better than ids/paths
                    try:
                        loc = frame.get_by_label(step["field_label"], exact=True).first
                        if loc.count() and loc.is_visible():
                            return loc
                    except Exception:
                        pass
                for cand in step["candidates"]:
                    try:
                        loc = frame.locator(cand).first
                        if loc.count() and loc.is_visible():
                            return loc
                    except Exception:
                        continue  # detached frame or a selector this page doesn't understand
            page = self._current_page(context)
            if page is None:
                return None
            page.wait_for_timeout(300)
            self._process_downloads()
        return None

    def _perform(self, loc, step: dict, start: date, end: date) -> None:
        if step["action"] == "click":
            loc.click(timeout=10_000)
        elif step["action"] == "select" and step.get("month_format"):
            # "November 2026" was right when recorded; pick the month the new period ends in.
            loc.select_option(label=end.strftime(step["month_format"]), timeout=5_000)
        elif step["action"] == "select":
            try:
                loc.select_option(label=step.get("option_text"), timeout=5_000)
            except Exception:
                loc.select_option(value=step.get("value"), timeout=5_000)
        elif step["action"] == "date":
            d = start if step.get("role") == "from" else end
            value = d.isoformat() if step.get("input_type") == "date" else format_date(d, step["format"], bool(step.get("upper")))
            try:
                loc.fill(value, timeout=3_000)
            except Exception:  # read-only date-picker fields: set the value the way the picker would
                loc.evaluate(
                    "(el, v) => { el.removeAttribute('readonly'); el.value = v;"
                    " el.dispatchEvent(new Event('input', {bubbles: true})); el.dispatchEvent(new Event('change', {bubbles: true})); }",
                    value,
                )

    def _settle(self, context) -> None:
        page = self._current_page(context)
        if page is None:
            return
        try:
            page.wait_for_load_state("domcontentloaded", timeout=15_000)
        except Exception:
            pass
        page.wait_for_timeout(600)
        self._process_downloads()

    # Downloads -> import
    def _process_downloads(self) -> None:
        while self._downloads:
            d = self._downloads.pop(0)
            try:
                name = d.suggested_filename or "statement"
                path = self._inbox_path(name)
                d.save_as(path)
            except Exception as exc:
                self._record_import(name, {"ok": False, "error": f"Download failed: {exc}"})
                continue
            self._import(path, name)
        while self._inline_pdfs:
            resp = self._inline_pdfs.pop(0)
            try:
                body = resp.body()
            except Exception:
                continue
            if len(body) < 1024:
                continue
            name = Path(resp.url.split("?")[0]).name or "statement.pdf"
            if not name.lower().endswith(".pdf"):
                name += ".pdf"
            path = self._inbox_path(name)
            path.write_bytes(body)
            self._import(path, name)

    def _inbox_path(self, name: str) -> Path:
        inbox = profiles.inbox_dir() / "netbanking"
        inbox.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", name)[:80]
        return inbox / f"{datetime.now():%Y%m%d-%H%M%S}_{safe}"

    def _import(self, path: Path, name: str) -> None:
        if path.suffix.lower() not in SUPPORTED:
            self._record_import(name, {"ok": False, "error": f"Not a statement file ({path.suffix}). Choose Excel or PDF format."})
            return
        self._set(message=f"Importing {name}…")
        password = get_pdf_password(self.account["id"])
        try:
            try:
                result = service.import_file(path, self.account["id"], password, name, source="netbanking")
            except NeedsAccount:
                result = service.import_file(path, None, password, name, source="netbanking")
            info = {"ok": True, **{k: v for k, v in result.as_dict().items() if k in (
                "account_name", "n_new", "n_duplicates", "duplicate_file", "period_start", "period_end", "categorize", "transfers")}}
        except PasswordRequired:
            info = {"ok": False, "error": f"This PDF is password-protected. Save the PDF password for {self.account['name']} in Settings "
                                          f"(it stays in {path.parent.name}/ and can be imported from the Import tab)."}
        except (StatementError, NeedsAccount, ValueError) as exc:
            info = {"ok": False, "error": str(exc)}
        self._record_import(name, info)
        if self.recording and info.get("ok"):
            self._stop_recording(save=True)
        elif self.state != "replaying":
            self._set(message=("Imported. Download more statements, or close the browser when you're done."
                               if info.get("ok") else "That download couldn't be imported. See below."))

    def _record_import(self, name: str, info: dict) -> None:
        with self._lock:
            self.imports.append({"file": name, "at": datetime.now().isoformat(timespec="seconds"), **info})
        if info.get("ok"):
            log.info("Net banking %s: downloaded %s imported (%s new)", self.bank, Path(name).suffix, info.get("n_new"))
        else:
            log.warning("Net banking %s: download %s not imported: %s", self.bank, Path(name).suffix, info.get("error"))


# ── One session at a time ─────────────────────────────────────────────────────

_current: NetBankingSession | None = None
_manager_lock = threading.Lock()


def start_session(account_id: int, headless: bool = False, channel: str | None = None) -> dict:
    global _current
    with _manager_lock:
        if _current and _current.active:
            if not _mine(_current):
                raise ValueError("Someone else is using net banking on this computer right now. Try again in a few minutes.")
            raise ValueError("A net banking window is already open. Close it first.")
        account = next((a for a in service.list_accounts() if a["id"] == account_id), None)
        if not account:
            raise service.NotFound(f"Account {account_id} not found")
        bank = account["bank"] if account["bank"] in BANKS else "OTHER"
        url = login_url(bank)
        if not url:
            raise ValueError("Set the bank's login page address first.")
        headless = headless or os.getenv("EXPENSE_TRACKER_BROWSER_HEADLESS") == "1"  # for automated checks only
        _current = NetBankingSession(account, url, headless=headless, channel=channel).start()
    return _current.snapshot()


def _mine(session: NetBankingSession | None) -> bool:
    """The browser window belongs to whoever opened it: nobody else sees or drives it."""
    if session is None:
        return False
    me, owner = profiles.current(), session.profile_user
    return (me.id if me else None) == (owner.id if owner else None)


def current_session() -> NetBankingSession | None:
    return _current


def session_status() -> dict | None:
    return _current.snapshot() if _mine(_current) else None


def session_command(name: str, timeout: float = 300, **kwargs) -> dict:
    if not _current or not _current.active or not _mine(_current):
        raise ValueError("No net banking window is open.")
    if name == "replay":
        start, end = kwargs.get("start"), kwargs.get("end")
        if not start or not end:
            start, end = default_period(_current.account["id"])
        kwargs = {"start": _as_date(start), "end": _as_date(end)}
        if kwargs["start"] > kwargs["end"]:
            raise ValueError("The start date is after the end date.")
        _current.command(name, **kwargs)  # long-running: report progress through session_status()
        return _current.snapshot()
    result = _current.command(name, **kwargs).result(timeout=timeout)
    return {"result": result, **_current.snapshot()}


def _as_date(v) -> date:
    return v if isinstance(v, date) else date.fromisoformat(str(v))
