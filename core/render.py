"""Render one clip: cut, frame it, optional zoom and progress bar, burn captions, even out the loudness,
encode on the GPU (NVENC) when possible.

Layouts (9:16 unless noted):
  auto        follow the speaker (smooth camera)
  fit         full frame on a blurred copy of itself
  fit_big     bigger frame (sides trimmed) on blur
  fit_black   full frame on black
  center      centre crop
  split_two   two speakers: left person on top, right person below
  split_wide  speaker on top, the full shot below
  split_game  (chosen automatically for auto / split_wide) gameplay stream: the streamer's webcam on top,
              the game below
"""
import os
import time
from pathlib import Path

from core import captions, media, reframe
from core.config import cfg

SIZES = {"9:16": (1080, 1920), "1:1": (1080, 1080), "16:9": (1920, 1080)}
ASPECTS = {"9:16": (9, 16), "1:1": (1, 1), "16:9": (16, 9)}
LAYOUTS = ["auto", "fit", "fit_big", "fit_black", "center", "split_two", "split_wide"]


def _opt(clip, opts, name, default):
    v = clip.get(name)
    return opts.get(name, default) if v is None else v


def _sharp(src_px, out_px):
    """A light sharpen when the picture is enlarged a lot, so zoomed crops don't look soft."""
    return ",unsharp=5:5:0.55:3:3:0.0" if out_px > src_px * 1.15 else ""


def _blur_bg(label_in, W, H, out):
    bw, bh = W // 4 // 2 * 2, H // 4 // 2 * 2
    return (f"[{label_in}]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},boxblur=14:2,"
            f"eq=brightness=-0.1:saturation=1.15,scale={W}:{H}[{out}]")


def _fit_on_blur(src_label, W, H, out, big=False):
    """Whole picture on a blurred, darkened copy of itself. big=True makes it larger (sides trimmed)."""
    fg = (f"scale={int(W * 1.35) // 2 * 2}:-2:flags=lanczos,crop='min(iw,{W})':ih" if big else
          f"scale={W}:{H}:force_original_aspect_ratio=decrease:flags=lanczos")
    return (f"[{src_label}]split=2[{out}_a][{out}_b];" + _blur_bg(f"{out}_a", W, H, f"{out}_bg") + ";"
            f"[{out}_b]{fg}[{out}_fg];[{out}_bg][{out}_fg]overlay=(W-w)/2:(H-h)/2[{out}]")


def _follow(cdir, name, target, centers, fps, cw, ch, sw, sh, W, H, zoom=None, y_center=None, src_label="0:v"):
    x, y, w, h = reframe.write_cmds(cdir / name, target, centers, fps, cw, ch, sw, sh, zoom, y_center)
    return (f"[{src_label}]sendcmd=f={name},{target}={w}:{h}:{x}:{y},"
            f"scale={W}:{H}:flags=lanczos{_sharp(ch, H)},setsar=1")


def render_clip(pdir: Path, proj, clip, transcript, on_progress):
    import numpy as np
    src = proj["source_file"]
    opts = proj["options"]
    fmt = opts.get("format", "9:16")
    W, H = SIZES.get(fmt, SIZES["9:16"])
    sw, sh = proj["width"], proj["height"]
    fps = min(float(proj.get("fps") or 30), 60.0)
    start, end = float(clip["start"]), float(clip["end"])
    dur = end - start
    cdir = pdir / "clips" / clip["id"]
    cdir.mkdir(parents=True, exist_ok=True)
    for old in cdir.glob("cmds*.txt"):
        old.unlink()
    layout = clip.get("layout") or opts.get("layout", "auto")
    zoom_on = bool(_opt(clip, opts, "zoom", False))
    bar_on = bool(_opt(clip, opts, "bar", False))
    bar_color = _opt(clip, opts, "bar_color", "FFFFFF")
    words = transcript.get("words") or []

    cw = reframe.crop_width(sw, sh, ASPECTS.get(fmt, (9, 16)))
    if layout.startswith("split") and fmt != "9:16":
        layout = "fit"                                 # two stacked frames only make sense when tall
    if cw >= sw - 2 and layout in ("auto", "center", "split_two", "split_wide"):
        layout = "fit"                                 # the source is already as narrow as the target
    n = int(dur * fps) + 1
    zoom = reframe.zoom_curve(reframe.zoom_points(words, start, end), fps, dur) if zoom_on else None
    _found = {}

    def find():                                        # faces are looked for once per clip
        if "s" not in _found:
            _found["s"] = reframe.detect(src, start, end, sw, sh,
                                         lambda f: on_progress(f * 0.3, "Finding the speakers..."))
        return _found["s"]

    chain = None
    if layout in ("auto", "split_wide") and fmt == "9:16":
        # gameplay with a streamer webcam: webcam on top, the game below (following a tiny webcam with a
        # tall crop, or showing it inside the whole frame, both look wrong)
        known = proj.get("cam_info") or {}           # what the first clip of this video found
        cam = rect = frames = None
        scale = 1.0
        if known.get("cam"):                           # same stream overlay in every clip: reuse it
            cam = tuple(v * d for v, d in zip(known["cam"], (sw, sh, sw, sh)))
            rect = tuple(v * d for v, d in zip(known["rect"], (sw, sh, sw, sh)))
        elif not known.get("none"):
            on_progress(0.0, "Looking for the streamer's webcam...")
            cam = reframe.find_facecam(find(), sw, sh)
            if cam:
                frames, scale = reframe.sample_frames(src, start, end, sw, sh)
                rect, borders = reframe.cam_rect(frames, scale, cam, sw, sh)  # the webcam's real border
                if borders < 2:                        # no overlay box: a wide shot (podcast), not a stream
                    cam = None
            clip["cam_info"] = ({"cam": [cam[0] / sw, cam[1] / sh, cam[2] / sw, cam[3] / sh],
                                 "rect": [rect[0] / sw, rect[1] / sh, rect[2] / sw, rect[3] / sh]}
                                if cam else {"none": True})
        if cam:
            H1 = int(H * 0.42) // 2 * 2
            H2 = H - H1
            tx, ty, tw, th = reframe.crop_in_rect(rect, cam, W, H1, sw)
            if tw < sw * 0.1:                                            # border found too tight: use the face
                tx, ty, tw, th = reframe.facecam_crop(cam, sw, sh, W, H1)
            if frames is None:                         # webcam reused from the first clip: frames for the bars
                frames, scale = reframe.sample_frames(src, start, end, sw, sh)
            bars = reframe.black_bars(frames, scale, sh, *(0, sw))
            gx, gy, gw, gh = reframe.game_crop2(rect, sw, sh, W, H2, bars)
            chain = (f"[0:v]split=2[s1][s2];"
                     f"[s1]crop={tw}:{th}:{tx}:{ty},scale={W}:{H1}:flags=lanczos{_sharp(th, H1)},setsar=1[t];"
                     f"[s2]crop={gw}:{gh}:{gx}:{gy},scale={W}:{H2}:flags=lanczos{_sharp(gh, H2)},setsar=1[b];"
                     f"[t][b]vstack=2,drawbox=x=0:y={H1 - 3}:w={W}:h=6:color=black@0.85:t=fill[v0]")
            layout = "split_game"
    if layout == "auto":
        on_progress(0.0, "Finding the speaker...")
        samples = find()
        if samples:
            centers = reframe.follow_one(samples, sw, cw, fps, dur)
            chain = _follow(cdir, "cmds.txt", "crop@a", centers, fps, cw, sh, sw, sh, W, H, zoom) + "[v0]"
        else:
            layout = "center"
    if layout in ("split_two", "split_wide"):
        H2 = H // 2
        ch = int(sh * 0.92) // 2 * 2
        cw2 = int(ch * W / H2) // 2 * 2
        if cw2 > sw:
            cw2 = sw // 2 * 2
            ch = int(cw2 * H2 / W) // 2 * 2
        samples = find()
        two = reframe.follow_two(samples, sw, cw2, fps, dur) if (samples and layout == "split_two") else None
        if layout == "split_two" and not two:
            layout = "split_wide"                      # only one person on screen
        if layout == "split_two":
            pl, pr, ly, ry = two
            top = _follow(cdir, "cmds_a.txt", "crop@a", pl, fps, cw2, ch, sw, sh, W, H2, zoom, ly, "s1")
            bot = _follow(cdir, "cmds_b.txt", "crop@b", pr, fps, cw2, ch, sw, sh, W, H2, zoom, ry, "s2")
            chain = f"[0:v]split=2[s1][s2];{top}[t];{bot}[b];[t][b]vstack=2[v0]"
        else:
            centers = (reframe.follow_one(samples, sw, cw2, fps, dur) if samples else np.full(n, sw / 2.0))
            top = _follow(cdir, "cmds_a.txt", "crop@a", centers, fps, cw2, ch, sw, sh, W, H2, zoom, None, "s1")
            chain = (f"[0:v]split=2[s1][s2];{top}[t];" + _fit_on_blur("s2", W, H2, "b") +
                     ";[t][b]vstack=2[v0]")
        chain = chain.replace("[v0]", f",drawbox=x=0:y={H2 - 3}:w={W}:h=6:color=black@0.85:t=fill[v0]") \
            if chain.endswith("vstack=2[v0]") else chain
    if chain is None and layout in ("fit", "fit_big"):
        chain = _fit_on_blur("0:v", W, H, "v0", big=layout == "fit_big")
    if chain is None and layout == "fit_black":
        chain = (f"[0:v]scale={W}:{H}:force_original_aspect_ratio=decrease:flags=lanczos,"
                 f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:black,setsar=1[v0]")
    if chain is None:
        layout = "center"
        centers = np.full(n, sw / 2.0)
        chain = _follow(cdir, "cmds.txt", "crop@a", centers, fps, cw, sh, sw, sh, W, H, zoom) + "[v0]"

    # captions + hook text (over the seam for split screens)
    ass = cdir / "captions.ass"
    has_ass = captions.build(words, start, end, W, H, clip.get("caption_style") or "Bold Pop", clip.get("hook", ""),
                             clip.get("hook_on", True), ass, pos="mid" if layout.startswith("split") else None)
    if not has_ass and ass.exists():
        ass.unlink()
    post = "[v0]setsar=1"
    if has_ass:
        rel = os.path.relpath(captions.FONTS, cdir).replace("\\", "/")
        post += f",ass=captions.ass:fontsdir='{rel}'"
    inputs = ["-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", src]
    if bar_on:
        bh_ = max(8, H // 160)
        inputs += ["-f", "lavfi", "-i", f"color=c=0x{bar_color}:s={W}x{bh_}:r={fps:.3f}"]
        post += f"[base];[base][1:v]overlay=x='-w+w*t/{dur:.3f}':y=H-h:eval=frame:shortest=1"
    thumb_on = bool(_opt(clip, opts, "thumbnail", True))
    extra_out = []
    if thumb_on:                                       # clean frames for the thumbnail, from the same render
        from core import thumbnail
        td = thumbnail.thumbs_dir(cdir)
        td.mkdir(exist_ok=True)
        for old in td.glob("tc_*.jpg"):
            old.unlink()
        tail, extra_out = thumbnail.candidate_output(dur)
        chain = chain + f";[v0]split=2[v0m][v0t];[v0t]{tail}[tc]"
        post = "[v0m]" + post[len("[v0]"):]
        extra_out = ["-map", "[tc]", *extra_out]
    graph = chain + ";" + post + ",format=yuv420p[v]"
    maps = ["-map", "[v]"]
    if proj.get("has_audio", True):
        graph += ";[0:a:0]loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[a]"
        maps += ["-map", "[a]", "-c:a", "aac", "-b:a", "192k"]

    tmp = cdir / "clip.tmp.mp4"
    base = 0.3 if layout in ("auto", "split_two", "split_wide", "split_game") else 0.0
    media.run([*inputs, "-filter_complex", graph, *maps, "-t", f"{dur:.3f}",
               *media.video_codec_args(cfg.get("render.encoder", "auto")), "-r", f"{fps:.3f}",
               "-movflags", "+faststart", tmp.name, *extra_out],
              cwd=cdir, duration=dur, on_progress=lambda f: on_progress(base + f * (0.97 - base), "Rendering..."))
    final = cdir / "clip.mp4"
    for i in range(20):
        try:
            os.replace(tmp, final)
            break
        except PermissionError:
            if i == 19:
                raise RuntimeError("clip.mp4 is open in another program. Close it and render again.") from None
            time.sleep(0.25)
    thumb = cdir / "thumb.jpg"
    media.run(["-ss", f"{min(1.5, dur / 2):.2f}", "-i", final, "-frames:v", "1", "-vf", "scale=360:-2",
               "-q:v", "3", thumb])
    cover = {}
    if thumb_on:
        on_progress(0.98, "Making the thumbnail...")
        try:
            cover = thumbnail.make(cdir, clip, layout, style=clip.get("cover_style") or opts.get("thumb_style", "Bold"),
                                   text=clip.get("cover_text"))
        except Exception as e:                         # a thumbnail problem never fails the clip
            from core.log import log
            log.warning("thumbnail for %s failed: %s", clip.get("id"), e)
    on_progress(1.0, "Done")
    clip.update(file=str(final), thumb=str(thumb), status="done", error="", layout_used=layout,
                rendered=time.time(), **cover)
    return clip
