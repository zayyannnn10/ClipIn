"""ClipIn cloud worker: runs inside GitHub Actions, one step per stage, so the phone can show progress
from the workflow's step list. Uses the same core code as the laptop app.

  python -m cloud.run download | transcribe | highlights | render | publish | fail

Job settings come from JOB_ID and JOB_OPTIONS (JSON from the phone). Keys come from repo secrets
(GEMINI_API_KEY, GROQ_API_KEY), never from the phone. Results are published to the `clips` branch:
  index.json                     newest jobs first (what the phone lists)
  jobs/<job>/result.json         clips, scores, AI titles and hashtags (or the error)
  jobs/<job>/<clip>.mp4 / .jpg   the clips and thumbnails
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import project  # noqa: E402
from core.log import log, setup  # noqa: E402

KEEP_JOBS = 20                                   # older jobs are removed from the clips branch
JOB = re.sub(r"[^A-Za-z0-9_-]", "", os.environ.get("JOB_ID", "local"))[:40] or "local"
PDIR = ROOT / "projects" / JOB
OPTS = {"length": "30-60", "count": 6, "format": "9:16", "layout": "auto", "caption_style": "Bold Pop",
        "hook_on": True, "zoom": False, "bar": False, "bar_color": "FFFFFF", "language": "auto", "focus": ""}


def say(msg):
    print(f"[ClipIn] {msg}", flush=True)


def load():
    return project.load(PDIR)


def save(p):
    project.save(PDIR, p)


def cookies():
    """Optional YT_COOKIES secret (cookies.txt text) for when YouTube blocks GitHub's servers."""
    text = os.environ.get("YT_COOKIES", "").strip()
    if text:
        f = ROOT / "config" / "yt_cookies.txt"
        f.parent.mkdir(exist_ok=True)
        f.write_text(text + "\n", encoding="utf-8")
        from core.config import cfg
        cfg.set("download.cookies_file", str(f))


# stages --------------------------------------------------------------------------------------------

def download():
    from core import download as dl, media
    opts = {**OPTS, **json.loads(os.environ.get("JOB_OPTIONS") or "{}")}
    url = str(opts.pop("url", "")).strip()
    if not dl.is_url(url):
        raise RuntimeError("That isn't a video link.")
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
          "created": time.time(), "options": opts, "status": "running", "stage": "download", "error": "",
          "source_file": str(path), "width": meta["width"], "height": meta["height"], "fps": meta["fps"],
          "has_audio": meta["has_audio"], "duration": info.get("duration") or meta["duration"], "clips": []})
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
                          o.get("focus", ""), lambda f, m="": say(m) if m else None)
    for c in clips:
        c.update(status="queued", layout=o.get("layout", "auto"), caption_style=o.get("caption_style", "Bold Pop"),
                 hook_on=bool(o.get("hook_on", True)))
    p["clips"], p["ai_used"] = clips, used
    save(p)
    say(f"{len(clips)} moments picked by {used}")


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
    if not any(c.get("status") == "done" for c in p["clips"]):
        raise RuntimeError("No clip could be rendered.")


# publishing to the clips branch -------------------------------------------------------------------

def git(*args, cwd, check=True):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()[:300]}")
    return r


def _remote():
    tok, repo = os.environ.get("GH_TOKEN", ""), os.environ.get("GITHUB_REPOSITORY", "")
    if tok and repo:                                 # Actions' own short-lived token (masked in logs)
        return f"https://x-access-token:{tok}@github.com/{repo}.git"
    return git("remote", "get-url", "origin", cwd=ROOT).stdout.strip()


def _publish(result, files):
    """Add this job to the clips branch (one squashed commit, newest KEEP_JOBS jobs). Retries if another
    job published at the same moment."""
    remote = _remote()
    for attempt in range(6):
        with tempfile.TemporaryDirectory() as tmp:
            w = Path(tmp)
            git("init", "-q", cwd=w)
            git("config", "user.name", "ClipIn cloud", cwd=w)
            git("config", "user.email", "clipin@users.noreply.github.com", cwd=w)
            git("remote", "add", "origin", remote, cwd=w)
            have = git("fetch", "-q", "--depth", "1", "origin", "clips", cwd=w, check=False).returncode == 0
            lease = ""
            if have:
                lease = git("rev-parse", "FETCH_HEAD", cwd=w).stdout.strip()
                git("checkout", "-q", "FETCH_HEAD", "--", ".", cwd=w)
            index = []
            if (w / "index.json").exists():
                index = json.loads((w / "index.json").read_text(encoding="utf-8"))
            index = [j for j in index if j.get("job") != JOB]
            index.insert(0, {"job": JOB, "title": result.get("title", ""), "created": result.get("created"),
                             "clips": len([c for c in result.get("clips", []) if c.get("file")]),
                             "error": result.get("error", ""),
                             "thumb": next((c["thumb"] for c in result.get("clips", []) if c.get("thumb")), "")})
            for old in index[KEEP_JOBS:]:
                shutil.rmtree(w / "jobs" / old["job"], ignore_errors=True)
            index = index[:KEEP_JOBS]
            jd = w / "jobs" / JOB
            jd.mkdir(parents=True, exist_ok=True)
            for src, name in files:
                shutil.copy2(src, jd / name)
            (jd / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
            (w / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
            # an orphan commit every time keeps the branch small (old clips don't pile up in history)
            git("checkout", "-q", "--orphan", "fresh", cwd=w)
            git("add", "-A", cwd=w)
            git("commit", "-q", "-m", f"clips for {JOB}", cwd=w)
            push = ["push", "-q", "origin", "HEAD:refs/heads/clips"]
            push.insert(2, f"--force-with-lease=clips:{lease}" if lease else "--force")
            if git(*push, cwd=w, check=False).returncode == 0:
                say(f"Published {len(files)} files to the clips branch")
                return
        say("Another job published at the same time, trying again...")
        time.sleep(3 + attempt * 2)
    raise RuntimeError("Couldn't publish the clips (too many jobs finishing at once).")


def publish():
    p = load()
    files, clips = [], []
    for c in p["clips"]:
        item = {k: c.get(k) for k in ("id", "title", "hook", "reason", "score", "start", "end", "caption_style",
                                      "status", "error", "yt")}
        item["duration"] = round(float(c["end"]) - float(c["start"]), 1)
        if c.get("status") == "done" and Path(c.get("file", "")).exists():
            files.append((c["file"], f"{c['id']}.mp4"))
            item["file"] = f"jobs/{JOB}/{c['id']}.mp4"
            if c.get("thumb") and Path(c["thumb"]).exists():
                files.append((c["thumb"], f"{c['id']}.jpg"))
                item["thumb"] = f"jobs/{JOB}/{c['id']}.jpg"
        clips.append(item)
    clips.sort(key=lambda c: -int(c.get("score") or 0))
    result = {"job": JOB, "title": p.get("title", ""), "channel": p.get("channel", ""), "source": p["source"],
              "duration": p.get("duration"), "created": p.get("created"), "finished": time.time(),
              "options": p.get("options", {}), "ai": p.get("ai_used", ""), "clips": clips, "error": ""}
    _publish(result, files)


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
    opts = json.loads(os.environ.get("JOB_OPTIONS") or "{}")
    _publish({"job": JOB, "title": title or opts.get("url", ""), "source": opts.get("url", ""),
              "created": time.time(), "finished": time.time(), "clips": [], "error": err}, [])


STAGES = {"download": download, "transcribe": transcribe, "highlights": highlights, "render": render,
          "publish": publish, "fail": fail}

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
