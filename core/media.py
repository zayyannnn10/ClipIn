"""FFmpeg helpers: find it, run it with progress, read a video's size/fps/length, pick the encoder."""
import functools
import os
import re
import shutil
import subprocess
import threading

NO_WINDOW = 0x08000000 if os.name == "nt" else 0


@functools.lru_cache(None)
def ffmpeg():
    p = shutil.which("ffmpeg")
    if p:
        return p
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def run(args, cwd=None, duration=None, on_progress=None):
    """Run ffmpeg. on_progress(fraction) is called while it works when duration (seconds) is given."""
    cmd = [ffmpeg(), "-hide_banner", "-nostdin", "-y", "-loglevel", "error"]
    if on_progress:
        cmd += ["-progress", "pipe:1", "-nostats"]
    cmd += [str(a) for a in args]
    p = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE if on_progress else subprocess.DEVNULL,
                         stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                         creationflags=NO_WINDOW)
    err = []
    t = threading.Thread(target=lambda: err.extend(p.stderr), daemon=True)
    t.start()
    if on_progress:
        for line in p.stdout:
            if line.startswith(("out_time_us=", "out_time_ms=")):
                try:
                    us = int(line.split("=", 1)[1])
                except ValueError:
                    continue
                if duration:
                    on_progress(min(1.0, us / 1e6 / duration))
    p.wait()
    t.join(timeout=2)
    if p.returncode != 0:
        msg = "".join(err).strip()
        raise RuntimeError("FFmpeg failed: " + (msg[-700:] or f"exit code {p.returncode}"))


def probe(path):
    """{'duration', 'width', 'height', 'fps', 'has_audio'} read from ffmpeg's description of the file."""
    r = subprocess.run([ffmpeg(), "-hide_banner", "-i", str(path)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    text = r.stderr
    info = {"duration": 0.0, "width": 0, "height": 0, "fps": 30.0, "has_audio": " Audio:" in text}
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", text)
    if m:
        info["duration"] = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
    for line in text.splitlines():
        if " Video:" in line:
            m = re.search(r"\b(\d{2,5})x(\d{2,5})\b", line)
            if m:
                info["width"], info["height"] = int(m[1]), int(m[2])
            m = re.search(r"([\d.]+) fps", line) or re.search(r"([\d.]+) tbr", line)
            if m:
                info["fps"] = float(m[1])
            rot = re.search(r"rotation of (-?[\d.]+)", text)
            if rot and abs(float(rot[1])) in (90.0, 270.0):
                info["width"], info["height"] = info["height"], info["width"]
            break
    if not info["width"]:
        raise RuntimeError("This file has no video picture that FFmpeg can read.")
    return info


@functools.lru_cache(None)
def nvenc_ok():
    try:
        r = subprocess.run([ffmpeg(), "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                            "-i", "color=c=black:s=256x256:d=0.2", "-c:v", "h264_nvenc", "-f", "null", "-"],
                           capture_output=True, timeout=30, creationflags=NO_WINDOW)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def video_codec_args(pref="auto"):
    if pref in ("auto", "nvenc") and nvenc_ok():
        return ["-c:v", "h264_nvenc", "-preset", "p6", "-tune", "hq", "-rc", "vbr", "-cq", "19", "-b:v", "0",
                "-maxrate", "20M", "-bufsize", "40M", "-profile:v", "high"]
    return ["-c:v", "libx264", "-preset", "fast", "-crf", "19", "-profile:v", "high"]
