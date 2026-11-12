"""Sign in to YouTube the normal way, in a real browser window that belongs only to ClipIn.

  1. sign_in()  opens Chrome (or Edge) with ClipIn's own browser profile (config/youtube-browser) at the
                YouTube sign-in page. The user signs in like on any website and closes the window.
  2. export()   starts that same profile hidden, asks the browser for its cookies over the local
                DevTools connection, and writes config/youtube_cookies.txt for yt-dlp.

The browser decrypts its own cookies, so this works with new Chrome/Edge versions that block other
programs from reading them. Nothing leaves the PC except the login going to Google/YouTube.
Before each download export() runs again, so yt-dlp always gets the browser's newest login."""
import asyncio
import json
import os
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

from core.log import log
from core.paths import CONFIG

PROFILE = CONFIG / "youtube-browser"
COOKIES = CONFIG / "youtube_cookies.txt"
SIGNIN_URL = "https://accounts.google.com/ServiceLogin?service=youtube&continue=https%3A%2F%2Fwww.youtube.com%2F"
NO_WINDOW = 0x08000000
BROWSERS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]
COMMON = ["--no-first-run", "--no-default-browser-check", "--disable-background-mode",
          "--disable-features=ChromeWhatsNewUI"]


def browser():
    return next((b for b in BROWSERS if Path(b).is_file()), None)


def signed_in_before():
    return PROFILE.is_dir()


def sign_in():
    """Open the sign-in window and wait until the user closes it."""
    exe = browser()
    if not exe:
        raise RuntimeError("Chrome or Edge is needed for sign-in, and neither was found.")
    PROFILE.mkdir(parents=True, exist_ok=True)
    subprocess.Popen([exe, f"--user-data-dir={PROFILE}", *COMMON, "--new-window", SIGNIN_URL])
    # Chrome often hands the window to another process and the one we started exits at once,
    # so wait for every browser process using ClipIn's profile to be gone instead.
    for _ in range(30):                  # give it up to 15 s to appear
        if profile_in_use():
            break
        time.sleep(0.5)
    deadline = time.time() + 30 * 60
    while profile_in_use():
        if time.time() > deadline:
            raise RuntimeError("The sign-in window was open for 30 minutes. Close it and try again.")
        time.sleep(1.0)
    time.sleep(1.5)                      # let the browser finish writing its profile


def profile_in_use():
    """True while any Chrome/Edge process is running with ClipIn's sign-in profile."""
    import psutil
    want = str(PROFILE).lower()
    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            if (proc.info["name"] or "").lower() in ("chrome.exe", "msedge.exe") and \
                    any(want in (a or "").lower() for a in proc.info["cmdline"] or []):
                return True
        except (psutil.Error, TypeError):
            continue
    return False


def _port_file():
    return PROFILE / "DevToolsActivePort"


async def _get_cookies(ws_url):
    import websockets
    async with websockets.connect(ws_url, max_size=50_000_000) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Storage.getCookies"}))
        while True:
            msg = json.loads(await ws.recv())
            if msg.get("id") == 1:
                cookies = msg.get("result", {}).get("cookies", [])
                break
        await ws.send(json.dumps({"id": 2, "method": "Browser.close"}))
        return cookies


def export():
    """Write config/youtube_cookies.txt from ClipIn's browser profile. Returns how many cookies were saved."""
    exe = browser()
    if not exe or not PROFILE.is_dir():
        raise RuntimeError("Not signed in yet.")
    if profile_in_use():
        raise RuntimeError("The ClipIn sign-in window is still open. Close it, then try again.")
    try:
        _port_file().unlink()
    except OSError:
        pass
    p = subprocess.Popen([exe, f"--user-data-dir={PROFILE}", *COMMON, "--headless=new",
                          "--remote-debugging-port=0", "--remote-allow-origins=*", "about:blank"],
                         creationflags=NO_WINDOW, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        port = None
        for _ in range(100):
            if _port_file().exists():
                lines = _port_file().read_text(encoding="utf-8").split()
                if lines:
                    port = int(lines[0])
                    break
            if p.poll() is not None:
                raise RuntimeError("The sign-in browser closed straight away. Close any open ClipIn sign-in "
                                   "window and try again.")
            time.sleep(0.1)
        if not port:
            raise RuntimeError("The sign-in browser didn't start.")
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=10) as r:
            ws_url = json.loads(r.read())["webSocketDebuggerUrl"]
        cookies = asyncio.run(_get_cookies(ws_url))
    finally:
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()
    keep = [c for c in cookies if c.get("domain", "").endswith(("youtube.com", "google.com"))]
    year = int(time.time()) + 365 * 86400
    lines = ["# Netscape HTTP Cookie File", "# Made by ClipIn AI from its own sign-in browser. Keep private.", ""]
    for c in keep:
        dom = c["domain"]
        exp = int(c.get("expires") or 0)
        lines.append("\t".join([dom, "TRUE" if dom.startswith(".") else "FALSE", c.get("path") or "/",
                                "TRUE" if c.get("secure") else "FALSE", str(exp if exp > 0 else year),
                                c["name"], c.get("value", "")]))
    tmp = COOKIES.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(COOKIES)
    log.info("YouTube sign-in: exported %d cookies", len(keep))
    return len(keep)


def sign_out():
    for path in (COOKIES,):
        try:
            path.unlink()
        except OSError:
            pass
    shutil.rmtree(PROFILE, ignore_errors=True)
