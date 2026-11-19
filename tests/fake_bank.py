"""A tiny fake net banking site for testing assisted fetch: login, framed menu, statement form, Excel download."""

import threading
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from helpers import hdfc_xlsx

PAGES = {
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
    "/statement.html": """<!doctype html><html><body>
<select name="selAcct" id="selAcct"><option value="">Select account</option><option value="A1">Savings XX1234</option></select>
<input id="frmDate" name="frmDate" placeholder="From (dd/mm/yyyy)">
<input id="toDate" name="toDate" readonly placeholder="To">
<div class="ui-datepicker" id="cal"><table><tr><td><a href="#" onclick="pick(); return false;">15</a></td></tr></table></div>
<select name="fmt" id="fmt"><option value="PDF">PDF</option><option value="XLS">Excel</option></select>
<button id="btnDownload" onclick="go()">Download</button>
<script>
  const pad = (n) => String(n).padStart(2, '0');
  function pick() { const d = new Date(); document.getElementById('toDate').value = pad(d.getDate()) + '/' + pad(d.getMonth() + 1) + '/' + d.getFullYear(); }
  function go() {
    const q = new URLSearchParams({ acct: selAcct.value, from: frmDate.value, to: toDate.value, fmt: fmt.value });
    location.href = '/download?' + q;
  }
</script></body></html>""",
}


class FakeBank:
    def __init__(self):
        self.downloads: list[dict] = []
        self._tmp = TemporaryDirectory()
        bank = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                url = urllib.parse.urlparse(self.path)
                if url.path in PAGES:
                    body = PAGES[url.path].encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif url.path == "/download":
                    q = dict(urllib.parse.parse_qsl(url.query))
                    bank.downloads.append(q)
                    body = bank.statement(q)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                    self.send_header("Content-Disposition", 'attachment; filename="Acct_Statement_XX1234.xlsx"')
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
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
