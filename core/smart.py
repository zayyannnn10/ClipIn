"""Smart mode for online videos: never download the whole video.

  1. look      read the video's details without downloading it: title, length, picture size,
               YouTube's "Most replayed" graph and its subtitles (which carry word timings)
  2. listen    no subtitles? download only the audio (about 1 MB a minute) and transcribe it
  3. moments   the AI picks the best moments from that transcript (core/highlights.py)
  4. clips     download only those moments (a minute or so each, sharp 1440p), transcribe each one
               for exact captions, render it, delete the piece

A one-hour video becomes a few small downloads instead of several gigabytes."""
import json
import shutil
import time
from pathlib import Path

from core import media
from core.log import log

MARGIN = 2.0                                   # extra seconds downloaded around each moment


def _ydl_opts():
    from core.download import _Logger
    return {"quiet": True, "no_warnings": True, "noplaylist": True, "logger": _Logger(),
            "js_runtimes": {"deno": {"path": None}, "node": {"path": None}}}


def look(url):
    """The video's details without downloading it."""
    from core.deps import require
    yt_dlp = require("yt_dlp")
    with yt_dlp.YoutubeDL(_ydl_opts()) as y:
        info = y.extract_info(url, download=False)
        words, lang = _caption_words(y, info)
    return info, words, lang


def _caption_words(ydl, info):
    """Word-timed transcript from YouTube's subtitles (json3), or ([], "") if there are none."""
    lang = (info.get("language") or "").split("-")[0]
    subs, auto = info.get("subtitles") or {}, info.get("automatic_captions") or {}
    order = []
    if lang:
        order += [(subs, lang), (subs, lang + "-orig"), (auto, lang + "-orig"), (auto, lang)]
    order += [(auto, k) for k in auto if k.endswith("-orig")]          # the spoken language's own captions
    order += [(subs, k) for k in subs if k.startswith("en")] + [(auto, "en")]
    for table, key in order:
        track = next((f for f in table.get(key) or [] if f.get("ext") == "json3"), None)
        if not track:
            continue
        try:
            data = json.loads(ydl.urlopen(track["url"]).read().decode("utf-8"))
            words = _parse_json3(data)
            if len(words) > 20:
                return words, key.replace("-orig", "")
        except Exception as e:
            log.warning("captions %s: %s", key, e)
    return [], ""


def _parse_json3(data):
    words = []
    for ev in data.get("events") or []:
        segs = ev.get("segs")
        if not segs:
            continue
        t0 = (ev.get("tStartMs") or 0) / 1000
        end = t0 + (ev.get("dDurationMs") or 0) / 1000
        timed = [(t0 + (s.get("tOffsetMs") or 0) / 1000, (s.get("utf8") or "").strip()) for s in segs]
        timed = [(t, w) for t, w in timed if w and w != "\n"]
        for i, (t, w) in enumerate(timed):
            nxt = timed[i + 1][0] if i + 1 < len(timed) else end
            for part in w.split():                 # a segment can hold more than one word
                words.append({"w": part, "s": round(t, 3), "e": round(max(t + 0.08, nxt), 3)})
    words.sort(key=lambda w: w["s"])
    out = []                                       # captions windows overlap: drop repeated words
    for w in words:
        if out and abs(out[-1]["s"] - w["s"]) < 0.01 and out[-1]["w"] == w["w"]:
            continue
        out.append(w)
    return out


def prepare(url, pdir: Path, language, on_progress):
    """Steps 1-2. Returns (meta, transcript) and writes pdir/transcript.json."""
    on_progress(0.05, "Reading the video (no download)...")
    info, words, cap_lang = look(url)
    duration = float(info.get("duration") or 0)
    meta = {"title": info.get("title") or "", "channel": info.get("uploader") or info.get("channel") or "",
            "duration": duration, "heatmap": info.get("heatmap") or [],
            "width": int(info.get("width") or 1920), "height": int(info.get("height") or 1080),
            "fps": float(info.get("fps") or 30), "has_audio": True}
    out = pdir / "transcript.json"
    if words and (language in (None, "", "auto") or cap_lang.startswith(language)):
        tr = {"language": cap_lang, "words": words, "segments": [], "engine": "youtube-captions"}
        out.write_text(json.dumps(tr, ensure_ascii=False), encoding="utf-8")
        on_progress(1.0, f"Read YouTube's subtitles ({len(words)} words) — no video download needed")
        return meta, tr
    # no usable subtitles: audio only
    from core import download, transcribe
    on_progress(0.1, "No subtitles — downloading only the audio...")
    audio = download.fetch_audio(url, pdir, lambda f, m="": on_progress(0.1 + f * 0.3, m))
    tr = transcribe.transcribe(str(audio), pdir, duration, language,
                               lambda f, m="": on_progress(0.4 + f * 0.6, m))
    try:
        audio.unlink()
    except OSError:
        pass
    return meta, tr


def fetch_piece(pdir: Path, p, clip, on_progress=None):
    """Download just this clip's moment (plus a little margin) into clips/<id>/piece. Returns (work, part, s0, s1)."""
    from core import download
    pdir = Path(pdir).resolve()
    s0 = max(0.0, float(clip["start"]) - MARGIN)
    s1 = float(clip["end"]) + MARGIN
    work = pdir / "clips" / clip["id"] / "piece"
    cb = on_progress or (lambda f, m="": None)
    last = None
    for fmt in (download.FMT_PIECE, download.FMT_PIECE_SAFE):   # sharp first, then the safest format
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        try:
            part, _ = download.fetch(p["source"], work, cb, section=(s0, s1), name="part", fmt=fmt)
            meta = media.probe(part)                  # a piece must have a picture and the whole moment
            if meta["duration"] < (s1 - s0) * 0.8:
                raise RuntimeError(f"the piece is only {meta['duration']:.0f}s of {s1 - s0:.0f}s")
            return work, part.resolve(), s0, s1
        except Exception as e:
            last = e
            log.warning("piece %s (%s) failed: %s", clip["id"], "sharp" if fmt == download.FMT_PIECE else "safe", e)
    raise RuntimeError(f"Couldn't download this moment: {last}")


def prefetch(pdir, p, clips, workers=3):
    """Start downloading every clip's moment in the background, a few at a time (YouTube sends each piece
    at about real-time speed, so several at once is much faster). Returns (pool, {clip id: future})."""
    from concurrent.futures import ThreadPoolExecutor
    pool = ThreadPoolExecutor(max_workers=workers)
    return pool, {c["id"]: pool.submit(fetch_piece, pdir, p, c) for c in clips}


def render_clip(pdir: Path, p, clip, transcript, on_progress, piece=None):
    """Step 4 for one clip: (fetch just this moment unless it's already here), exact captions, render."""
    from core import render, transcribe
    pdir = Path(pdir).resolve()
    if piece is None:
        on_progress(0.02, "Downloading just this moment...")
        piece = fetch_piece(pdir, p, clip, lambda f, m="": on_progress(0.02 + f * 0.25, m))
    work, part, s0, s1 = piece
    try:
        meta = media.probe(part)
        on_progress(0.28, "Writing exact captions...")
        try:                                       # exact words for this clip from its own audio
            tr = transcribe.transcribe(str(part), work, s1 - s0, p["options"].get("language", "auto"),
                                       lambda f, m="": None)
            words = tr.get("words") or []
        except Exception as e:                     # fall back to the whole-video transcript
            log.warning("clip transcript: %s", e)
            words = [{**w, "s": w["s"] - s0, "e": w["e"] - s0} for w in transcript.get("words") or []
                     if s0 <= w["s"] <= s1]
        p2 = {**p, "source_file": str(part), "width": meta["width"], "height": meta["height"],
              "fps": meta["fps"], "has_audio": meta["has_audio"]}
        c2 = {**clip, "start": float(clip["start"]) - s0, "end": float(clip["end"]) - s0}
        if clip.get("tease"):                      # the cold-open moment, in the piece's own time
            c2["tease"] = {k: float(clip["tease"][k]) - s0 for k in ("start", "end")}
        render.render_clip(pdir, p2, c2, {"words": words}, lambda f, m="": on_progress(0.32 + f * 0.68, m))
        clip.update({k: c2[k] for k in ("file", "thumb", "status", "error", "layout_used", "rendered", "tease_used")
                     if k in c2})
        clip.update({k: c2[k] for k in ("cover", "cover_text", "cover_style", "cover_frame", "cover_frames", "cam_info") if k in c2})
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return clip
