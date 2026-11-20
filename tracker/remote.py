"""Phone access through Tailscale: is it set up, and turning it on or off.

The app itself only ever listens on this computer (config.HOST). Phones reach it through
Tailscale: `tailscale serve --bg <port>` gives this computer a private HTTPS address
(https://<computer>.<tailnet>.ts.net) that only devices in your tailnet, or people you share
this one computer with, can open. Tailscale passes requests on to http://127.0.0.1:<port>.

This module only runs the official `tailscale` command line; it never changes Tailscale's
settings beyond `serve` for this app's port. Turning it on or off is allowed only from this
computer itself (app.py checks), never from a phone.
"""

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path

import config

log = logging.getLogger(__name__)

_WINDOWS_CLI = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Tailscale" / "tailscale.exe"


def cli() -> str | None:
    found = shutil.which("tailscale")
    if found:
        return found
    return str(_WINDOWS_CLI) if _WINDOWS_CLI.exists() else None


def _run(args: list[str], timeout: float = 20) -> tuple[int, str]:
    """Run the tailscale CLI -> (exit code, output). Exit code 124 means it was still waiting when we stopped it."""
    exe = cli()
    if not exe:
        return 127, "Tailscale isn't installed on this computer."
    try:
        p = subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or b""
        out = out.decode(errors="replace") if isinstance(out, bytes) else out
        return 124, out
    except OSError as exc:
        return 126, str(exc)


def _json(args: list[str]) -> dict | None:
    code, out = _run(args)
    if code != 0:
        return None
    try:
        return json.loads(out or "{}")
    except ValueError:
        return None


def _proxies_this_app(serve: dict) -> bool:
    targets = {f"http://127.0.0.1:{config.PORT}", f"http://localhost:{config.PORT}", f"127.0.0.1:{config.PORT}",
               f"localhost:{config.PORT}", str(config.PORT)}
    for site in (serve.get("Web") or {}).values():
        for handler in (site.get("Handlers") or {}).values():
            if (handler.get("Proxy") or "").rstrip("/") in targets:
                return True
    return False


def status() -> dict:
    """What a phone needs: Tailscale installed, signed in, and serving this app over HTTPS."""
    out = {"installed": cli() is not None, "signed_in": False, "serving": False, "address": None, "port": config.PORT}
    if not out["installed"]:
        return out
    st = _json(["status", "--json"]) or {}
    out["signed_in"] = st.get("BackendState") == "Running"
    dns = ((st.get("Self") or {}).get("DNSName") or "").rstrip(".")
    if dns:
        out["address"] = f"https://{dns}/"
    if out["signed_in"]:
        out["serving"] = _proxies_this_app(_json(["serve", "status", "--json"]) or {})
    return out


def turn_on() -> dict:
    """`tailscale serve --bg <port>`. The first time, Tailscale may ask to enable HTTPS for your tailnet:
    its message (with the link to do that) is passed back to show on the page."""
    code, output = _run(["serve", "--bg", str(config.PORT)], timeout=20)
    log.info("Phone access: turn on -> exit %s", code)
    result = status()
    result["output"] = output.strip()[-1500:]
    result["ok"] = result["serving"]
    return result


def turn_off() -> dict:
    code, output = _run(["serve", "--https=443", "off"], timeout=20)
    log.info("Phone access: turn off -> exit %s", code)
    result = status()
    result["output"] = output.strip()[-1500:]
    result["ok"] = not result["serving"]
    return result
