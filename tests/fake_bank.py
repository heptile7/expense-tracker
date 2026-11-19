"""A tiny fake net banking site for testing assisted fetch.

Mirrors what real banks do that broke naive recording:
  * login page, framed menu, statement form;
  * auto-numbered field ids (bb_input_7...) that change on every visit, with <label>s;
  * a month dropdown ("November 2026") next to the date fields;
  * a read-only "To" date set by a calendar pop-up without a change event;
  * the download started by a script-created <a> that the page clicks itself.
"""

import threading
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from helpers import hdfc_xlsx

STATIC = {
    "/login.html": """<!doctype html><html><body><h1>Test Bank NetBanking</h1>
<form onsubmit="location.href='/home.html'; return false;">
  <input id="userid" name="fldLoginUserId" placeholder="Customer ID">
  <input id="password" name="fldPassword" type="password">
  <input id="otp" name="fldOtp" placeholder="One-time password">
  <button id="login" type="submit">Login</button>
</form></body></html>""",
    "/home.html": """<!doctype html><html><body>
<iframe name="left_menu" src="/menu.html" width="220" height="400"></iframe>
<iframe name="main_part" src="/welcome.html" width="900" height="600"></iframe>
</body></html>""",
    "/menu.html": """<!doctype html><html><body>
<a href="/welcome.html" target="main_part">Home</a><br>
<a href="/statement.html" target="main_part">Account Statement</a><br>
<a href="/login.html" target="_top">Log out</a></body></html>""",
    "/welcome.html": "<!doctype html><html><body><p>Welcome back</p></body></html>",
}

STATEMENT = """<!doctype html><html><body>
<select name="selAcct" id="selAcct"><option value="">Select account</option><option value="A1">Savings XX1234</option></select>
<label for="bb_input___A__">Statement period</label>
<select id="bb_input___A__"><option value="0: Object">Select period</option><option value="1: Object">October 2026</option>
  <option value="2: Object">November 2026</option><option value="3: Object">December 2026</option></select>
<label for="bb_input___B__">From date</label><input id="bb_input___B__" placeholder="dd/mm/yyyy">
<label for="bb_input___C__">To date</label><input id="bb_input___C__" readonly>
<div class="ui-datepicker" id="cal"><table><tr><td><a href="#" onclick="pick(); return false;">15</a></td></tr></table></div>
<select name="fmt" id="fmt"><option value="PDF">PDF</option><option value="XLS">Excel</option></select>
<button id="btnDownload" onclick="go()">Download</button>
<script>
  const pad = (n) => String(n).padStart(2, '0');
  const el = (id) => document.getElementById(id);
  function pick() { const d = new Date(); el('bb_input___C__').value = pad(d.getDate()) + '/' + pad(d.getMonth() + 1) + '/' + d.getFullYear(); }
  function go() {
    const period = el('bb_input___A__');
    const q = new URLSearchParams({ acct: el('selAcct').value, period: period.options[period.selectedIndex].text,
      from: el('bb_input___B__').value, to: el('bb_input___C__').value, fmt: el('fmt').value });
    const a = document.createElement('a');  // like HDFC: a hidden link the page clicks itself
    a.href = '/download?' + q; a.download = 'statement.xlsx';
    document.body.appendChild(a); a.click(); a.remove();
  }
</script></body></html>"""


class FakeBank:
    def __init__(self):
        self.downloads: list[dict] = []
        self.statement_visits = 0
        self._tmp = TemporaryDirectory()
        bank = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, body: bytes, ctype: str, extra: dict | None = None):
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                url = urllib.parse.urlparse(self.path)
                if url.path in STATIC:
                    self._send(STATIC[url.path].encode(), "text/html; charset=utf-8")
                elif url.path == "/statement.html":
                    n = 7 + 10 * bank.statement_visits  # ids shift on every visit, like Angular apps
                    bank.statement_visits += 1
                    html = STATEMENT.replace("__A__", str(n)).replace("__B__", str(n + 1)).replace("__C__", str(n + 2))
                    self._send(html.encode(), "text/html; charset=utf-8")
                elif url.path == "/download":
                    q = dict(urllib.parse.parse_qsl(url.query))
                    bank.downloads.append(q)
                    self._send(bank.statement(q), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                               {"Content-Disposition": 'attachment; filename="Acct_Statement_XX1234.xlsx"'})
                else:
                    self.send_error(404)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def statement(self, q: dict) -> bytes:
        start = datetime.strptime(q["from"], "%d/%m/%Y")
        end = datetime.strptime(q["to"], "%d/%m/%Y")
        n = len(self.downloads)
        rows = [
            (start.strftime("%d/%m/%y"), f"UPI-GREENLEAF ENTERPRISES-GREENLEAF@OKAXIS-UTIB0000001-61234567{n:04d}-UPI", f"61234567{n:04d}", 380.0, None, 9620.0),
            (end.strftime("%d/%m/%y"), f"UPI-SWIGGY-SWIGGY@ICICI-ICIC0DC0099-61234568{n:04d}-UPI", f"61234568{n:04d}", 220.0, None, 9400.0),
        ]
        path = hdfc_xlsx(Path(self._tmp.name) / f"s{n}.xlsx", rows)
        return path.read_bytes()

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self._tmp.cleanup()
