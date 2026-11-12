"""YouTube sign-in for downloads: check that a cookies.txt file or a browser holds a logged-in YouTube session.

Nothing is sent anywhere here; the cookies are only read on this PC. yt-dlp later sends them to YouTube
when it downloads (see core/download.py)."""
import http.cookiejar
from pathlib import Path

from core.config import cfg

LOGIN_COOKIES = ("LOGIN_INFO", "__Secure-3PSID", "__Secure-1PSID", "SAPISID", "SID")


def _logged_in(jar):
    names = {c.name for c in jar if "youtube.com" in c.domain or "google.com" in c.domain}
    return any(n in names for n in LOGIN_COOKIES)


def check_file(path):
    p = Path(path)
    if not p.is_file():
        return False, "That cookies.txt file doesn't exist."
    jar = http.cookiejar.MozillaCookieJar(str(p))
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except (OSError, http.cookiejar.LoadError) as e:
        return False, f"That file isn't a cookies.txt in Netscape format ({e})."
    if not _logged_in(jar):
        return False, "The file has no YouTube login. Export it again while signed in on youtube.com."
    return True, "Connected with cookies.txt"


def check_browser(browser):
    from core.download import _cookie_error
    from yt_dlp.cookies import extract_cookies_from_browser
    try:
        jar = extract_cookies_from_browser(browser)
    except Exception as e:
        return False, _cookie_error(str(e))
    if not _logged_in(jar):
        return False, f"No YouTube login found in {browser.title()}. Sign in on youtube.com there first."
    return True, f"Connected with {browser.title()}"


def check():
    f = (cfg.get("download.cookies_file") or "").strip()
    if f:
        return check_file(f)
    b = (cfg.get("download.cookies_browser") or "none").lower()
    if b != "none":
        return check_browser(b)
    return False, "Not connected"
