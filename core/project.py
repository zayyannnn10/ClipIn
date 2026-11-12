"""A project = one source video and its clips, kept in projects/<id>/project.json.

Layout of a project folder:
  project.json        settings, status, clip list
  source.mp4          the downloaded video (local files are used where they are)
  transcript.json     words with timestamps
  clips/<cNN>/        clip.mp4, thumb.jpg, captions.ass, cmds.txt
"""
import json
import os
import shutil
import time
import uuid
from pathlib import Path

from core.paths import PROJECTS


def create(source, options, title=""):
    pid = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
    d = PROJECTS / pid
    d.mkdir(parents=True)
    save(d, {"id": pid, "source": source, "title": title, "created": time.time(), "options": options,
             "status": "queued", "stage": "", "error": "", "clips": []})
    return d


def load(d):
    return json.loads((Path(d) / "project.json").read_text(encoding="utf-8"))


def save(d, data):
    p = Path(d) / "project.json"
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    for i in range(10):                       # the window may be reading it at this moment
        try:
            os.replace(tmp, p)
            return
        except PermissionError:
            time.sleep(0.05 * (i + 1))
    os.replace(tmp, p)


def all_projects():
    out = []
    for d in PROJECTS.iterdir():
        if (d / "project.json").is_file():
            try:
                out.append((d, load(d)))
            except (OSError, ValueError):
                continue
    out.sort(key=lambda x: x[1].get("created", 0), reverse=True)
    return out


def delete(d):
    shutil.rmtree(d)


def clip(data, cid):
    return next((c for c in data.get("clips", []) if c.get("id") == cid), None)


def fmt_time(s):
    s = max(0, int(round(float(s or 0))))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def safe_name(text, limit=60):
    keep = "".join(ch if ch.isalnum() or ch in " -_.,()" else " " for ch in (text or ""))
    return " ".join(keep.split())[:limit].strip(" .") or "clip"
