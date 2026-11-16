"""The `clips` branch of the phone repository: where finished clips, results and job states live.
Used by the GitHub cloud worker and by the laptop (when it makes clips for the phone).

  index.json                 newest jobs first; each has a status:
                               done          clips are ready
                               error         something failed (see "error")
                               laptop        YouTube blocked the cloud; waiting for the laptop
                               laptop-working  the laptop is making the clips ("stage" says what it's doing)
  jobs/<job>/request.json    the link and options the phone sent (for the laptop)
  jobs/<job>/result.json     clips, scores, AI titles and hashtags
  jobs/<job>/<clip>.mp4/.jpg the clips and thumbnails

Every change is one squashed orphan commit, so old clips never pile up in the repository's history."""
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

KEEP_JOBS = 20
NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _git(*args, cwd, check=True):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, creationflags=NO_WINDOW)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {args[0]}: {r.stderr.strip()[-300:]}")
    return r


def update(remote, change, say=print, message="update"):
    """Fetch the branch, let change(workdir, index) edit files and return the new index, push.
    Retries when someone else pushed at the same moment."""
    for attempt in range(6):
        with tempfile.TemporaryDirectory() as tmp:
            w = Path(tmp)
            _git("init", "-q", cwd=w)
            _git("config", "user.name", "ClipIn", cwd=w)
            _git("config", "user.email", "clipin@users.noreply.github.com", cwd=w)
            _git("remote", "add", "origin", remote, cwd=w)
            have = _git("fetch", "-q", "--depth", "1", "origin", "clips", cwd=w, check=False).returncode == 0
            lease = ""
            if have:
                lease = _git("rev-parse", "FETCH_HEAD", cwd=w).stdout.strip()
                _git("checkout", "-q", "FETCH_HEAD", "--", ".", cwd=w)
            index = json.loads((w / "index.json").read_text(encoding="utf-8")) if (w / "index.json").exists() else []
            index = change(w, index)
            for old in index[KEEP_JOBS:]:
                shutil.rmtree(w / "jobs" / old["job"], ignore_errors=True)
            index = index[:KEEP_JOBS]
            (w / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
            _git("checkout", "-q", "--orphan", "fresh", cwd=w)
            _git("add", "-A", cwd=w)
            _git("commit", "-q", "-m", message, cwd=w)
            push = ["push", "-q", f"--force-with-lease=clips:{lease}" if lease else "--force", "origin",
                    "HEAD:refs/heads/clips"]
            if _git(*push, cwd=w, check=False).returncode == 0:
                return index
        say("Someone else saved at the same moment, trying again...")
        time.sleep(3 + attempt * 2)
    raise RuntimeError("Couldn't save to the clips branch (too many jobs at once).")


def _entry(index, job):
    return next((j for j in index if j.get("job") == job), None)


def set_entry(remote, job, say=print, request=None, **fields):
    """Create or update a job's line in index.json (and optionally store its request)."""
    def change(w, index):
        e = _entry(index, job)
        if not e:
            e = {"job": job, "created": time.time(), "clips": 0, "error": "", "thumb": ""}
            index.insert(0, e)
        e.update(fields)
        if request is not None:
            d = w / "jobs" / job
            d.mkdir(parents=True, exist_ok=True)
            (d / "request.json").write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
        return index
    return update(remote, change, say, f"{job}: {fields.get('status', 'update')}")


def publish(remote, job, result, files, say=print):
    """Save a finished (or failed) job: clips, thumbnails, result.json, and its index line."""
    def change(w, index):
        d = w / "jobs" / job
        d.mkdir(parents=True, exist_ok=True)
        for src, name in files:
            shutil.copy2(src, d / name)
        (d / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        index = [j for j in index if j.get("job") != job]
        index.insert(0, {"job": job, "title": result.get("title", ""), "created": result.get("created"),
                         "clips": len([c for c in result.get("clips", []) if c.get("file")]),
                         "error": result.get("error", ""), "status": "error" if result.get("error") else "done",
                         "by": result.get("by", "cloud"),
                         "thumb": next((c["thumb"] for c in result.get("clips", []) if c.get("thumb")), "")})
        index.sort(key=lambda j: -(j.get("created") or 0))
        return index
    update(remote, change, say, f"clips for {job}")
    say(f"Published {len(files)} files")


def build(job, p, by="cloud"):
    """result.json + the files to publish, from a ClipIn project (cloud or laptop)."""
    files, clips = [], []
    for c in p.get("clips", []):
        item = {k: c.get(k) for k in ("id", "title", "hook", "reason", "score", "start", "end", "caption_style",
                                      "status", "error", "yt")}
        item["duration"] = round(float(c["end"]) - float(c["start"]), 1)
        if c.get("status") == "done" and Path(c.get("file", "")).exists():
            files.append((c["file"], f"{c['id']}.mp4"))
            item["file"] = f"jobs/{job}/{c['id']}.mp4"
            if c.get("thumb") and Path(c["thumb"]).exists():
                files.append((c["thumb"], f"{c['id']}.jpg"))
                item["thumb"] = f"jobs/{job}/{c['id']}.jpg"
        clips.append(item)
    clips.sort(key=lambda c: -int(c.get("score") or 0))
    result = {"job": job, "title": p.get("title", ""), "channel": p.get("channel", ""), "source": p.get("source", ""),
              "duration": p.get("duration"), "created": p.get("created"), "finished": time.time(),
              "options": p.get("options", {}), "ai": p.get("ai_used", ""), "clips": clips, "error": "", "by": by}
    return result, files
