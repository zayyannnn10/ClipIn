"""Download a YouTube (or any yt-dlp supported) video as source.mp4, up to 1080p."""
import re
import time
from pathlib import Path

from core import media
from core.log import log

VIDEO_EXT = (".mp4", ".mkv", ".webm", ".mov", ".m4v")


def is_url(text):
    return bool(re.match(r"https?://", (text or "").strip(), re.I))


class _Logger:
    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        log.warning("yt-dlp: %s", msg)

    def error(self, msg):
        log.error("yt-dlp: %s", msg)


# 1080p H.264 decodes fastest and merges cleanly into MP4; 1440p gives sharper vertical crops but is huge
# for long videos (70 minutes = 3.4 GB), so it's only used up to LONG_VIDEO seconds
FMT_1080 = "bv*[height<=1080][vcodec^=avc1]+ba[ext=m4a]/bv*[height<=1080]+ba/b[height<=1080]/b"
FMT_1440 = "bv*[height<=1440][height>1080]+ba/" + FMT_1080
LONG_VIDEO = 20 * 60

CLIENT_FALLBACKS = [["tv_simply"], ["web_safari"], ["mweb"], ["tv"], ["android_vr"], ["web_embedded"]]


def _run(yt_dlp, opts, url):
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=True)


def _needs_signin(msg):
    m = msg.lower()
    return "sign in" in m or "not a bot" in m or "cookies" in m or "login required" in m


def _cookie_opts():
    from core import youtube_signin
    from core.config import cfg
    f = (cfg.get("download.cookies_file") or "").strip().strip('"')
    if f and Path(f) == youtube_signin.COOKIES and youtube_signin.signed_in_before():
        try:
            youtube_signin.export()            # fresh login from ClipIn's sign-in browser
        except Exception as e:
            log.warning("Couldn't refresh the YouTube login: %s", e)
    if f and Path(f).is_file():
        return {"cookiefile": f}
    b = (cfg.get("download.cookies_browser") or "none").lower()
    if b and b != "none":
        return {"cookiesfrombrowser": (b, None, None, None)}
    return {}


def _cookie_error(msg):
    m = msg.lower()
    if "could not copy" in m and "cookie database" in m:
        return ("Couldn't read the browser's YouTube login: the browser keeps its cookie file locked while it's "
                "open. Close the browser completely and press Retry, or use a cookies.txt file (Settings).")
    if "decrypt" in m or "dpapi" in m or "app-bound" in m:
        return ("Couldn't read the browser's YouTube login: newer Chrome/Edge encrypt their cookies. "
                "Use a cookies.txt file instead (Settings → YouTube sign-in).")
    if _needs_signin(msg):
        return "YouTube still asks to sign in. Make sure you're logged in to YouTube in that browser, or use a fresh cookies.txt."
    return f"Couldn't download the video: {msg[:300]}"


def fetch(url, outdir: Path, on_progress, section=None, name="source"):
    """Returns (path, info). on_progress(fraction, message).
    section=(start, end) downloads only that part of the video (used to re-render a clip)."""
    import yt_dlp

    seen = {"files": 0}

    def hook(d):
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            if total:
                part = "video" if seen["files"] == 0 else "audio"
                frac = min(1.0, (d.get("downloaded_bytes") or 0) / total)
                mb = total / 1_048_576
                on_progress(frac, f"Downloading {part}: {frac * 100:.0f}% of {mb:.0f} MB")
        elif d.get("status") == "finished":
            seen["files"] += 1

    opts = {
        # YouTube's player check needs a JavaScript runtime; Node.js or Deno is used if installed
        "js_runtimes": {"deno": {"path": None}, "node": {"path": None}},
        "outtmpl": str(outdir / f"{name}.%(ext)s"),
        "format": FMT_1440,                    # long videos switch to FMT_1080 below
        "merge_output_format": "mp4",
        "ffmpeg_location": media.ffmpeg(),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "progress_hooks": [hook],
        "logger": _Logger(),
        "retries": 5,
        "fragment_retries": 5,
        "concurrent_fragment_downloads": 4,
    }
    if section:
        from yt_dlp.utils import download_range_func
        opts["download_ranges"] = download_range_func(None, [section])
        # no re-encode: MP4 edit lists make playback start at the asked time, and it takes seconds, not minutes
    # The YouTube login is only a backup: sending it with every download makes YouTube answer
    # "The page needs to be reloaded" for many videos. It's used below only when YouTube asks to sign in.
    if not section:
        try:                                   # a quick look first: long videos get the smaller 1080p file
            with yt_dlp.YoutubeDL({**opts, "skip_download": True, "progress_hooks": []}) as y:
                pre = y.extract_info(url, download=False)
            if (pre.get("duration") or 0) > LONG_VIDEO:
                opts["format"] = FMT_1080
        except Exception:
            pass                               # the real download below reports any problem
    else:
        opts["format"] = FMT_1080
    info, msg = None, ""
    try:
        info = _run(yt_dlp, opts, url)
    except Exception as e:
        msg = str(e).replace("ERROR: ", "")
        if not _needs_signin(msg) and "429" not in msg:
            raise RuntimeError(f"Couldn't download the video: {msg[:300]}") from None
    if info is None:
        # YouTube treats its different players differently; on cloud servers some still get through
        for clients in CLIENT_FALLBACKS:
            on_progress(0.0, f"YouTube blocked the normal player, trying the {clients[0]} player...")
            time.sleep(2)
            try:
                info = _run(yt_dlp, {**opts, "extractor_args": {"youtube": {"player_client": clients}}}, url)
                log.info("Downloaded with the %s player", clients[0])
                break
            except Exception as e:
                msg = str(e).replace("ERROR: ", "")
                log.warning("%s player: %s", clients[0], msg[:160])
    if info is None:
        extra = _cookie_opts()
        if not extra:
            raise RuntimeError("YouTube asked to sign in (bot check). Open Settings → YouTube sign-in and pick "
                               "your browser or a cookies.txt file, then press Retry.") from None
        on_progress(0.0, "YouTube asked to sign in, retrying with your browser's YouTube login...")
        try:
            info = _run(yt_dlp, {**opts, **extra}, url)
        except Exception as e2:
            raise RuntimeError(_cookie_error(str(e2).replace("ERROR: ", ""))) from None
    vids = sorted((p for p in outdir.glob(f"{name}.*") if p.suffix.lower() in VIDEO_EXT),
                  key=lambda p: p.stat().st_size, reverse=True)
    if not vids:
        raise RuntimeError("The download finished but no video file was found.")
    return vids[0], {
        "title": info.get("title") or "",
        "duration": float(info.get("duration") or 0),
        "channel": info.get("uploader") or info.get("channel") or "",
        "heatmap": info.get("heatmap") or [],
    }
