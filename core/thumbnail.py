"""Thumbnails made from the clip itself.

While a clip renders, FFmpeg also saves CANDIDATES clean frames (same framing, no captions or bar) into
clips/<id>/thumbs/. pick() scores them (sharp, a big face, good light) and compose() turns the best one into a
cover: punchy text in a thick outline with one highlighted word, placed where it doesn't hide the face.

  cover.jpg       1080x1920, for the clip card, downloads and the YouTube thumbnail
"""
import re
from pathlib import Path

from core.log import log
from core.paths import ROOT

CANDIDATES = 14
FONTS = ROOT / "fonts"
STYLES = {                                   # name: (font file, highlight colour, uppercase)
    "Bold":   ("Montserrat-Black.ttf", (255, 216, 74), True),
    "Clean":  ("Anton-Regular.ttf", (255, 255, 255), True),
    "Pop":    ("Bangers-Regular.ttf", (255, 122, 182), True),
    "Neon":   ("BebasNeue-Regular.ttf", (90, 240, 255), True),
    "Soft":   ("Poppins-ExtraBold.ttf", (198, 244, 50), False),
}
STOP = {"the", "a", "an", "and", "or", "to", "of", "in", "on", "is", "it", "this", "that", "for", "with", "my", "i",
        "you", "your", "we", "be", "was", "are", "at", "so"}


def thumbs_dir(cdir: Path):
    return Path(cdir) / "thumbs"


def candidate_output(dur):
    """Extra FFmpeg output (filter tail + output args) that saves the candidate frames during the render."""
    rate = max(0.05, CANDIDATES / max(dur, 1.0))
    return f"fps={rate:.4f},format=yuvj420p", ["-fps_mode", "vfr", "-q:v", "2", "thumbs/tc_%02d.jpg"]


def short_text(clip):
    """Thumbnail words: the AI's own thumbnail text, else the hook, else the title, trimmed to 5 words."""
    t = (clip.get("thumb_text") or clip.get("hook") or clip.get("title") or "").strip()
    t = re.sub(r"[\"“”]", "", t)
    words = t.split()
    if len(words) > 5:                        # keep the strongest start of the sentence
        words = words[:5]
    return " ".join(words).rstrip(".,;:")


def _faces(img_bgr):
    """[(x, y, w, h, score)] with the same face model the camera uses."""
    from core import reframe
    if not reframe.available():
        return []
    import cv2
    h, w = img_bgr.shape[:2]
    s = 480 / max(h, w)
    small = cv2.resize(img_bgr, (int(w * s), int(h * s)))
    det = cv2.FaceDetectorYN.create(str(reframe.MODEL), "", (small.shape[1], small.shape[0]), 0.6, 0.3, 50)
    _, found = det.detect(small)
    return [(f[0] / s, f[1] / s, f[2] / s, f[3] / s, float(f[-1])) for f in (found if found is not None else [])]


def score_frame(path):
    import cv2
    import numpy as np
    img = cv2.imread(str(path))
    if img is None:
        return -1, None
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    sharp = cv2.Laplacian(cv2.resize(gray, (360, 640)), cv2.CV_64F).var()
    light = float(gray.mean())
    faces = _faces(img)
    h, w = gray.shape
    big = max((f[2] * f[3] for f in faces), default=0) / (w * h)
    conf = max((f[4] for f in faces), default=0)
    s = (min(sharp, 900) / 900) * 0.4 + min(big / 0.04, 1.0) * 0.35 + conf * 0.15 \
        + (0.1 if 55 < light < 200 else 0.0)
    face = max(faces, key=lambda f: f[2] * f[3]) if faces else None
    return s, face


def candidates(cdir):
    return sorted(thumbs_dir(cdir).glob("tc_*.jpg"))


def pick(cdir):
    """(best frame path, its face box) from the saved candidates, best first order saved in ranking."""
    ranked = []
    for f in candidates(cdir):
        s, face = score_frame(f)
        if s >= 0:
            ranked.append((s, str(f), face))
    ranked.sort(key=lambda r: -r[0])
    return ranked


def _font(name, size):
    from PIL import ImageFont
    try:
        return ImageFont.truetype(str(FONTS / name), size)
    except OSError:
        return ImageFont.load_default()


def _wrap(draw, words, font, max_w):
    lines, cur = [], []
    for wd in words:
        test = " ".join(cur + [wd])
        if cur and draw.textlength(test, font=font) > max_w:
            lines.append(cur)
            cur = [wd]
        else:
            cur.append(wd)
    if cur:
        lines.append(cur)
    return lines


def compose(frame_path, text, out_path, style="Bold", face=None, layout="", size=(1080, 1920)):
    """Draw the cover: the frame, a soft shade behind the text, big outlined text with one highlighted word."""
    from PIL import Image, ImageDraw, ImageEnhance, ImageFilter
    W, H = size
    img = Image.open(frame_path).convert("RGB")
    face_y = None if face is None else (face[1] + face[3] / 2) / img.height    # 0 top .. 1 bottom
    img = img.resize((W, H), Image.LANCZOS)
    img = ImageEnhance.Contrast(img).enhance(1.08)
    img = ImageEnhance.Color(img).enhance(1.15)
    img = ImageEnhance.Sharpness(img).enhance(1.25)
    font_file, hl, upper = STYLES.get(style, STYLES["Bold"])
    words = []
    for wd in (text.upper() if upper else text).split():   # keep "GTA 6", "a 1" etc. on one line
        if words and (len(wd) <= 2 or wd.isdigit()):
            words[-1] += " " + wd
        else:
            words.append(wd)
    if not words:
        img.save(out_path, quality=92)
        return out_path
    # where the text goes: away from the face; on a webcam + game split, over the seam
    if layout == "split_game":
        zone = 0.47
    elif face_y is not None and face_y < 0.5:
        zone = 0.70
    else:
        zone = 0.22
    key = max((w for w in words if re.sub(r"\W", "", w).lower() not in STOP), key=len, default=words[0])
    draw = ImageDraw.Draw(img)
    max_w = int(W * 0.88)
    size_px = 190
    while size_px > 70:
        font = _font(font_file, size_px)
        lines = _wrap(draw, words, font, max_w)
        if len(lines) <= 3 and all(draw.textlength(" ".join(l), font=font) <= max_w for l in lines):
            break
        size_px -= 8
    font = _font(font_file, size_px)
    lines = _wrap(draw, words, font, max_w)
    line_h = int(size_px * 1.08)
    block_h = line_h * len(lines)
    y0 = int(H * zone - block_h / 2)
    y0 = max(int(H * 0.05), min(y0, int(H * 0.92) - block_h))
    # soft dark shade behind the text so it reads on any picture
    shade = Image.new("L", (W, H), 0)
    sd = ImageDraw.Draw(shade)
    sd.rectangle([0, y0 - int(size_px * 0.6), W, y0 + block_h + int(size_px * 0.6)], fill=150)
    shade = shade.filter(ImageFilter.GaussianBlur(size_px * 0.6))
    img = Image.composite(Image.new("RGB", (W, H), (0, 0, 0)), img, shade)
    draw = ImageDraw.Draw(img)
    stroke = max(6, size_px // 11)
    y = y0
    for line in lines:
        widths = [draw.textlength(w, font=font) for w in line]
        space = draw.textlength(" ", font=font)
        x = (W - (sum(widths) + space * (len(line) - 1))) / 2
        for w, wd in zip(widths, line):
            col = hl if wd == key and hl != (255, 255, 255) else (255, 255, 255)
            if wd == key and hl == (255, 255, 255):    # white style: the key word gets a red bar instead
                draw.rectangle([x - 8, y + line_h * 0.12, x + w + 8, y + line_h * 0.98], fill=(230, 40, 60))
            draw.text((x + 5, y + 7), wd, font=font, fill=(0, 0, 0), stroke_width=stroke, stroke_fill=(0, 0, 0))
            draw.text((x, y), wd, font=font, fill=col, stroke_width=stroke, stroke_fill=(0, 0, 0))
            x += w + space
        y += line_h
    img.save(out_path, quality=92)
    return out_path


def make(cdir, clip, layout="", style="Bold", text=None, frame=None):
    """Pick the best frame (or the one asked for) and write cover.jpg. Returns the clip fields to store."""
    cdir = Path(cdir)
    ranked = pick(cdir)
    if not ranked:
        return {}
    if frame is not None:
        chosen = next((r for r in ranked if Path(r[1]).name == frame), ranked[0])
    else:
        chosen = ranked[0]
    text = short_text(clip) if text is None else text
    out = cdir / "cover.jpg"
    compose(chosen[1], text, out, style, chosen[2], layout)
    log.info("Thumbnail for %s: %s, \"%s\" (%s)", clip.get("id"), Path(chosen[1]).name, text, style)
    return {"cover": str(out), "cover_text": text, "cover_style": style, "cover_frame": Path(chosen[1]).name,
            "cover_frames": [Path(r[1]).name for r in ranked]}
