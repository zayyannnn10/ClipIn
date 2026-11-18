"""ClipIn cloud worker: runs inside GitHub Actions, one step per stage, so the phone can show progress
from the workflow's step list. Uses the same core code as the laptop app.

  python -m cloud.run check | download | transcribe | highlights | render | publish | fail

`check` asks YouTube about the video first (seconds). If YouTube blocks GitHub's servers, the job is handed
to the laptop (status "laptop" in the clips branch) and the remaining steps are skipped.
Keys come from repo secrets (GEMINI_API_KEY, GROQ_API_KEY), never from the phone.
"""
import json
import os
import re
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import clipsbranch, project  # noqa: E402
from core.log import log, setup  # noqa: E402

JOB = re.sub(r"[^A-Za-z0-9_-]", "", os.environ.get("JOB_ID", "local"))[:40] or "local"
PDIR = ROOT / "projects" / JOB
OPTS = {"length": "30-60", "count": 6, "format": "9:16", "layout": "auto", "caption_style": "Bold Pop",
        "hook_on": True, "zoom": False, "bar": False, "bar_color": "FFFFFF", "language": "auto", "focus": ""}
CREATED = time.time()


def say(msg):
    print(f"[ClipIn] {msg}", flush=True)


def request():
    return {**OPTS, **json.loads(os.environ.get("JOB_OPTIONS") or "{}")}


def remote():
    tok, repo = os.environ.get("GH_TOKEN", ""), os.environ.get("GITHUB_REPOSITORY", "")
    if tok and repo:                                 # Actions' own short-lived token (masked in logs)
        return f"https://x-access-token:{tok}@github.com/{repo}.git"
    import subprocess
    return subprocess.run(["git", "remote", "get-url", "origin"], cwd=ROOT, capture_output=True,
                          text=True).stdout.strip()


def load():
    return project.load(PDIR)


def save(p):
    project.save(PDIR, p)


def output(name, value):
    f = os.environ.get("GITHUB_OUTPUT")
    if f:
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(f"{name}={value}\n")


def cookies():
    """Optional YT_COOKIES secret (cookies.txt text)."""
    text = os.environ.get("YT_COOKIES", "").strip()
    if text:
        f = ROOT / "config" / "yt_cookies.txt"
        f.parent.mkdir(exist_ok=True)
        f.write_text(text + "\n", encoding="utf-8")
        from core.config import cfg
        cfg.set("download.cookies_file", str(f))


# stages --------------------------------------------------------------------------------------------

def check():
    """Can GitHub's servers get this video? If YouTube blocks them, hand the job to the laptop."""
    import yt_dlp
    from core import download as dl
    req = request()
    url = str(req.get("url", "")).strip()
    if not dl.is_url(url):
        raise RuntimeError("That isn't a video link.")
    cookies()
    base = {"quiet": True, "no_warnings": True, "noplaylist": True, "logger": dl._Logger(),
            "js_runtimes": {"deno": {"path": None}, "node": {"path": None}}}
    blocked, title = False, ""
    for extra in [{}] + [{"extractor_args": {"youtube": {"player_client": c}}} for c in dl.CLIENT_FALLBACKS[:2]]:
        try:
            with yt_dlp.YoutubeDL({**base, **extra}) as y:
                info = y.extract_info(url, download=False)
            title = info.get("title") or ""
            blocked = False
            break
        except Exception as e:
            msg = str(e)
            if dl._needs_signin(msg) or "429" in msg:
                blocked = True
                continue
            raise RuntimeError(f"Couldn't open the video: {msg.replace('ERROR: ', '')[:300]}") from None
    if blocked:
        say("YouTube blocks GitHub's servers for this video. Handing it to your laptop.")
        clipsbranch.set_entry(remote(), JOB, say, request=req, title=title or url, status="laptop",
                              stage="Sent to your laptop", created=CREATED)
        output("blocked", "true")
    else:
        say(f"Video OK: {title}")
        clipsbranch.set_entry(remote(), JOB, say, title=title or url, status="working",
                              stage="Downloading the video", created=CREATED)
        output("blocked", "false")


def download():
    from core import download as dl, media
    opts = request()
    url = str(opts.pop("url", "")).strip()
    PDIR.mkdir(parents=True, exist_ok=True)
    cookies()
    say(f"Downloading {url}")
    last = [0.0]

    def prog(f, m=""):
        if time.time() - last[0] > 5:
            last[0] = time.time()
            say(m or f"{f * 100:.0f}%")
    path, info = dl.fetch(url, PDIR, prog)
    meta = media.probe(path)
    save({"id": JOB, "source": url, "title": info.get("title") or "", "channel": info.get("channel", ""),
          "created": CREATED, "options": opts, "status": "running", "stage": "download", "error": "",
          "source_file": str(path), "width": meta["width"], "height": meta["height"], "fps": meta["fps"],
          "has_audio": meta["has_audio"], "duration": info.get("duration") or meta["duration"], "clips": [],
          "heatmap": info.get("heatmap") or []})
    if not meta["has_audio"]:
        raise RuntimeError("This video has no sound, so there's nothing to transcribe.")
    say(f"Video ready: {info.get('title')} ({project.fmt_time(meta['duration'])})")


def transcribe():
    from core import transcribe as tr
    p = load()
    tr.transcribe(p["source_file"], PDIR, p["duration"], p["options"].get("language", "auto"),
                  lambda f, m="": say(m) if m else None)
    t = json.loads((PDIR / "transcript.json").read_text(encoding="utf-8"))
    say(f"{len(t.get('words', []))} words ({t.get('engine')})")


def highlights():
    from core import highlights as hl
    p = load()
    t = json.loads((PDIR / "transcript.json").read_text(encoding="utf-8"))
    o = p["options"]
    clips, used = hl.find(t, p.get("title", ""), int(o.get("count", 6)), o.get("length", "30-60"),
                          o.get("focus", ""), lambda f, m="": say(m) if m else None, heatmap=p.get("heatmap"))
    for c in clips:
        c.update(status="queued", layout=o.get("layout", "auto"), caption_style=o.get("caption_style", "Bold Pop"),
                 hook_on=bool(o.get("hook_on", True)))
    p["clips"], p["ai_used"] = clips, used
    save(p)
    say(f"{len(clips)} moments picked by {used}")
    partial(p, f"Picked {len(clips)} moments — making the clips")


def partial(p, stage):
    """Publish what's ready so far, so the phone shows clips one by one."""
    try:
        result, files = clipsbranch.build(JOB, p, by="cloud")
        clipsbranch.publish(remote(), JOB, result, files, say, status="working", stage=stage)
    except clipsbranch.Cancelled:
        raise
    except Exception as e:
        say(f"(couldn't update the phone yet: {e})")


def render():
    from core import metadata, render as rd
    p = load()
    t = json.loads((PDIR / "transcript.json").read_text(encoding="utf-8"))
    for i, c in enumerate(p["clips"], 1):
        say(f"Clip {i} of {len(p['clips'])}: {c['title']}")
        try:
            rd.render_clip(PDIR, p, c, t, lambda f, m="": None)
        except Exception as e:
            log.error("clip %s failed: %s", c["id"], traceback.format_exc())
            c.update(status="error", error=str(e)[:300])
            say(f"  failed: {e}")
            continue
        try:                                       # AI title, description and Shorts hashtags
            c["yt"] = metadata.generate(c, t, p.get("title", ""))
        except Exception as e:
            say(f"  no AI hashtags: {e}")
        save(p)
        partial(p, f"{i} of {len(p['clips'])} clips ready")
    if not any(c.get("status") == "done" for c in p["clips"]):
        raise RuntimeError("No clip could be rendered.")


def publish():
    result, files = clipsbranch.build(JOB, load(), by="cloud")
    clipsbranch.publish(remote(), JOB, result, files, say)


def fail():
    """Called when a stage failed: publish the reason so the phone can show it."""
    err = "Something went wrong."
    f = ROOT / "work_error.txt"
    if f.exists():
        err = f.read_text(encoding="utf-8").strip() or err
    title = ""
    try:
        title = load().get("title", "")
    except Exception:
        pass
    req = request()
    clipsbranch.publish(remote(), JOB, {"job": JOB, "title": title or req.get("url", ""), "source": req.get("url", ""),
                                        "created": CREATED, "finished": time.time(), "clips": [], "error": err}, [], say)


STAGES = {"check": check, "download": download, "transcribe": transcribe, "highlights": highlights,
          "render": render, "publish": publish, "fail": fail}

if __name__ == "__main__":
    setup("cloud", console=True)
    stage = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        STAGES[stage]()
    except Exception as e:
        msg = str(e)[:500] or e.__class__.__name__
        say(f"ERROR: {msg}")
        traceback.print_exc()
        if stage != "fail":
            (ROOT / "work_error.txt").write_text(f"{stage}: {msg}" if stage not in msg else msg, encoding="utf-8")
        sys.exit(1)
