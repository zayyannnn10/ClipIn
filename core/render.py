"""Render one clip: cut, frame (follow face / fit on blur or black / centre), optional zoom cuts and progress
bar, burn captions, even out the loudness, encode on the GPU (NVENC) when possible."""
import os
import time
from pathlib import Path

from core import captions, media, reframe
from core.config import cfg

SIZES = {"9:16": (1080, 1920), "1:1": (1080, 1080), "16:9": (1920, 1080)}
ASPECTS = {"9:16": (9, 16), "1:1": (1, 1), "16:9": (16, 9)}


def _opt(clip, opts, name, default):
    v = clip.get(name)
    return opts.get(name, default) if v is None else v


def render_clip(pdir: Path, proj, clip, transcript, on_progress):
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
    layout = clip.get("layout") or opts.get("layout", "auto")
    zoom_on = bool(_opt(clip, opts, "zoom", False))
    bar_on = bool(_opt(clip, opts, "bar", False))
    bar_color = _opt(clip, opts, "bar_color", "FFFFFF")
    words = transcript.get("words") or []

    # captions + hook text
    ass = cdir / "captions.ass"
    has_ass = captions.build(words, start, end, W, H, clip.get("caption_style") or "Bold Pop", clip.get("hook", ""),
                             clip.get("hook_on", True), ass)
    if not has_ass and ass.exists():
        ass.unlink()

    # framing
    cw = reframe.crop_width(sw, sh, ASPECTS.get(fmt, (9, 16)))
    if cw >= sw - 2 and layout in ("auto", "center"):
        layout = "fit"                         # the source is already as narrow as the target
    zooms = reframe.zoom_points(words, start, end) if zoom_on else None
    chain = None
    if layout == "auto":
        on_progress(0.0, "Finding the speaker...")
        keys = reframe.track(src, start, end, sw, sh, cw, lambda f: on_progress(f * 0.3, "Finding the speaker..."))
        if keys:
            x0 = reframe.write_cmds(keys, fps, dur, cdir / "cmds.txt", cw, sh, sw, zooms)
            chain = f"[0:v]sendcmd=f=cmds.txt,crop={cw}:{sh}:{x0}:0,scale={W}:{H}:flags=lanczos,setsar=1"
    if chain is None and layout in ("fit", "fit_black"):
        if layout == "fit_black":
            chain = (f"[0:v]scale={W}:{H}:force_original_aspect_ratio=decrease:flags=lanczos,"
                     f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:black,setsar=1")
        else:
            bw, bh = W // 4 // 2 * 2, H // 4 // 2 * 2
            chain = (f"[0:v]split=2[a][b];"
                     f"[a]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},boxblur=12:2,"
                     f"eq=brightness=-0.08,scale={W}:{H}[bg];"
                     f"[b]scale={W}:{H}:force_original_aspect_ratio=decrease:flags=lanczos[fg];"
                     f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1")
    if chain is None:
        layout = "center"
        x0 = (sw - cw) // 2
        if zooms and len(zooms) > 1:
            reframe.write_cmds([(0.0, float(x0))], fps, dur, cdir / "cmds.txt", cw, sh, sw, zooms)
            chain = f"[0:v]sendcmd=f=cmds.txt,crop={cw}:{sh}:{x0}:0,scale={W}:{H}:flags=lanczos,setsar=1"
        else:
            chain = f"[0:v]crop={cw}:{sh}:{x0}:0,scale={W}:{H}:flags=lanczos,setsar=1"
    if has_ass:
        chain += ",ass=captions.ass"
    inputs = ["-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", src]
    if bar_on:
        bh_ = max(8, H // 160)
        inputs += ["-f", "lavfi", "-i", f"color=c=0x{bar_color}:s={W}x{bh_}:r={fps:.3f}"]
        chain += (f"[base];[base][1:v]overlay=x='-w+w*t/{dur:.3f}':y=H-h:eval=frame:shortest=1")
    graph = chain + ",format=yuv420p[v]"
    maps = ["-map", "[v]"]
    if proj.get("has_audio", True):
        graph += ";[0:a:0]loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[a]"
        maps += ["-map", "[a]", "-c:a", "aac", "-b:a", "160k"]

    tmp = cdir / "clip.tmp.mp4"
    base = 0.3 if layout == "auto" else 0.0
    media.run([*inputs, "-filter_complex", graph, *maps, "-t", f"{dur:.3f}",
               *media.video_codec_args(cfg.get("render.encoder", "auto")), "-r", f"{fps:.3f}",
               "-movflags", "+faststart", tmp.name],
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
    on_progress(1.0, "Done")
    clip.update(file=str(final), thumb=str(thumb), status="done", error="", layout_used=layout,
                rendered=time.time())
    return clip
