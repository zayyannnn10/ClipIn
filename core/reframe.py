"""Auto-framing: follow faces so a wide video can be cropped to vertical (or split into two frames).

Faces are found 5 times a second with OpenCV's YuNet detector (models/face_detection_yunet_2023mar.onnx).
The camera path is then made smooth like a real camera operator's:
  - while the speaker hardly moves, the shot holds perfectly still (no wobble)
  - when they move, the camera glides (Gaussian-smoothed path with a speed limit, no jerks)
  - on a scene cut it jumps straight to the new position (a pan across a cut looks wrong)
  - zoom-ins ease in and out over 0.4 s instead of jumping
The result is an FFmpeg 'sendcmd' file that sets the crop for every frame."""
import numpy as np

from core.log import log
from core.paths import ROOT

SAMPLES_PER_SEC = 5
DETECT_HEIGHT = 360
MODEL = ROOT / "models" / "face_detection_yunet_2023mar.onnx"
MODEL_URL = ("https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/"
             "face_detection_yunet_2023mar.onnx")


def available():
    return MODEL.is_file() and MODEL.stat().st_size > 50_000


def download_model():
    import requests
    MODEL.parent.mkdir(exist_ok=True)
    r = requests.get(MODEL_URL, timeout=60)
    r.raise_for_status()
    if len(r.content) < 50_000:
        raise RuntimeError("The face model download looks incomplete.")
    tmp = MODEL.with_suffix(".tmp")
    tmp.write_bytes(r.content)
    tmp.replace(MODEL)
    return len(r.content)


def crop_width(src_w, src_h, aspect):
    aw, ah = aspect
    return min(src_w, int(src_h * aw / ah) // 2 * 2)


# face detection --------------------------------------------------------------------------------------

def detect(src, start, end, src_w, src_h, on_progress):
    """[(t, [(x, y, w, h), ...])] five times a second, or None (no model / no faces)."""
    if not available():
        log.info("Reframe: face model not installed, using the centre")
        return None
    import cv2
    cap = cv2.VideoCapture(str(src))
    if not cap.isOpened():
        return None
    cap.set(cv2.CAP_PROP_POS_MSEC, start * 1000)
    scale = DETECT_HEIGHT / src_h
    size = (max(1, int(src_w * scale)), DETECT_HEIGHT)
    det = cv2.FaceDetectorYN.create(str(MODEL), "", size, 0.6, 0.3, 5000)
    det.setInputSize(size)
    dur = end - start
    step, t_next = 1 / SAMPLES_PER_SEC, 0.0
    samples = []
    while True:
        if not cap.grab():
            break
        t = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000 - start
        if t > dur:
            break
        if t + 1e-6 < t_next:
            continue
        t_next += step
        ok, frame = cap.retrieve()
        if not ok:
            continue
        _, found = det.detect(cv2.resize(frame, size))
        faces = [(f[0] / scale, f[1] / scale, f[2] / scale, f[3] / scale) for f in (found if found is not None else [])]
        samples.append((max(0.0, t), faces))
        on_progress(min(1.0, t / max(dur, 0.1)))
    cap.release()
    if not samples or not any(f for _, f in samples):
        log.info("Reframe: no faces found")
        return None
    return samples


# smoothing --------------------------------------------------------------------------------------------

def _gauss(x, sigma):
    if sigma < 0.5 or len(x) < 3:
        return x.copy()
    r = int(3 * sigma)
    k = np.exp(-0.5 * (np.arange(-r, r + 1) / sigma) ** 2)
    k /= k.sum()
    pad = np.pad(x, r, mode="edge")
    return np.convolve(pad, k, mode="valid")


def _limit_speed(x, maxv):
    """Clamp how far the camera may move per frame (forward and backward pass, averaged)."""
    f = x.copy()
    for i in range(1, len(f)):
        f[i] = f[i - 1] + np.clip(f[i] - f[i - 1], -maxv, maxv)
    b = x.copy()
    for i in range(len(b) - 2, -1, -1):
        b[i] = b[i + 1] + np.clip(b[i] - b[i + 1], -maxv, maxv)
    return (f + b) / 2


def smooth_path(times, values, fps, dur, sw, cw):
    """Per-frame camera centres from 5-per-second face positions."""
    n = int(dur * fps) + 1
    tf = np.arange(n) / fps
    t = np.asarray(times, float)
    x = np.asarray(values, float)
    if len(x) > 4:                                    # median of 5 removes single wrong detections
        x = np.array([np.median(x[max(0, i - 2):i + 3]) for i in range(len(x))])
    cuts = [t[i] for i in range(1, len(x))            # big jump that stays = scene cut
            if abs(x[i] - x[i - 1]) > 0.22 * sw and (i + 1 >= len(x) or abs(x[i + 1] - x[i - 1]) > 0.22 * sw)]
    dense = np.interp(tf, t, x)
    out = np.empty(n)
    bounds = [0] + [min(n, max(0, int(round(c * fps)))) for c in cuts] + [n]
    for a, b in zip(bounds[:-1], bounds[1:]):
        seg = dense[a:b]
        if not len(seg):
            continue
        lo, hi = np.percentile(seg, [10, 90])
        if hi - lo < 0.2 * cw:
            out[a:b] = np.median(seg)                 # hardly moves: hold the shot still
        else:
            s = _gauss(seg, 0.5 * fps)                 # glide...
            s = _limit_speed(s, 1.2 * cw / fps)       # ...no faster than ~1.2 crop widths per second...
            near = _gauss(seg, 0.15 * fps)
            s = np.clip(s, near - 0.3 * cw, near + 0.3 * cw)   # ...but never let the face leave the middle 60%
            out[a:b] = _gauss(s, 0.2 * fps)
    return out


def _fill(raw):
    first = next((v for _, v in raw if v is not None), None)
    if first is None:
        return None
    out, last = [], first
    for t, v in raw:
        last = v if v is not None else last
        out.append((t, last))
    return out


def follow_one(samples, sw, cw, fps, dur):
    """Smooth per-frame centre of the main speaker (sticks with the current one unless another is much bigger)."""
    raw, cur = [], None
    for t, faces in samples:
        if not faces:
            raw.append((t, None))
            continue
        faces = sorted(faces, key=lambda f: -f[2] * f[3])
        best = faces[0]
        if cur is not None and len(faces) > 1:
            near = min(faces, key=lambda f: abs(f[0] + f[2] / 2 - cur))
            if best[2] * best[3] < 1.6 * near[2] * near[3]:
                best = near
        cur = best[0] + best[2] / 2
        raw.append((t, cur))
    filled = _fill(raw)
    return smooth_path([t for t, _ in filled], [v for _, v in filled], fps, dur, sw, cw)


def follow_two(samples, sw, cw, fps, dur):
    """Two speakers (left and right): per-frame centres for each, plus their face heights' centres.
    None when the clip doesn't really have two people."""
    both = sum(1 for _, f in samples if len(f) >= 2)
    if both < 0.3 * len(samples):
        return None
    left, right, ly, ry = [], [], [], []
    last_l = last_r = None
    for t, faces in samples:
        faces = sorted(faces, key=lambda f: -f[2] * f[3])[:2]
        if len(faces) == 2:
            a, b = sorted(faces, key=lambda f: f[0])
            last_l, last_r = a, b
        elif len(faces) == 1 and last_l is not None:
            f = faces[0]
            cx = f[0] + f[2] / 2
            if abs(cx - (last_l[0] + last_l[2] / 2)) < abs(cx - (last_r[0] + last_r[2] / 2)):
                last_l = f
            else:
                last_r = f
        left.append((t, None if last_l is None else last_l[0] + last_l[2] / 2))
        right.append((t, None if last_r is None else last_r[0] + last_r[2] / 2))
        if last_l is not None:
            ly.append(last_l[1] + last_l[3] / 2)
            ry.append(last_r[1] + last_r[3] / 2)
    fl, fr = _fill(left), _fill(right)
    if not fl or not fr:
        return None
    pl = smooth_path([t for t, _ in fl], [v for _, v in fl], fps, dur, sw, cw)
    pr = smooth_path([t for t, _ in fr], [v for _, v in fr], fps, dur, sw, cw)
    return pl, pr, float(np.median(ly)), float(np.median(ry))


# zoom ------------------------------------------------------------------------------------------------

def zoom_points(words, start, end, factor=1.12, min_len=1.6):
    """Zoom changes at sentence starts: alternate normal / zoomed in. Returns [(t, zoom)]."""
    pts, last, z, new_sentence, prev_e = [(0.0, 1.0)], 0.0, 1.0, False, None
    for w in words:
        if w["e"] <= start or w["s"] >= end:
            continue
        t = max(0.0, w["s"] - start)
        if prev_e is not None and w["s"] - prev_e > 0.45:
            new_sentence = True                    # a pause also starts a new thought
        if new_sentence and t - last >= min_len:
            z = factor if z == 1.0 else 1.0
            pts.append((t, z))
            last = t
        new_sentence = w["w"][-1:] in ".!?"
        prev_e = w["e"]
    return pts


def zoom_curve(zooms, fps, dur, ease=0.4):
    """Per-frame zoom; every change eases in and out over `ease` seconds (smoothstep)."""
    n = int(dur * fps) + 1
    z = np.ones(n)
    if not zooms:
        return z
    prev = zooms[0][1]
    z[:] = prev
    for t0, target in zooms[1:]:
        a = int(t0 * fps)
        b = min(n, a + max(1, int(ease * fps)))
        if a >= n:
            break
        s = np.linspace(0, 1, b - a)
        z[a:b] = prev + (target - prev) * (s * s * (3 - 2 * s))
        z[b:] = target
        prev = target
    return z


# FFmpeg commands -------------------------------------------------------------------------------------

def write_cmds(path, target, centers, fps, cw, ch, sw, sh, zoom=None, y_center=None):
    """One crop command per frame for the crop filter named `target` (e.g. crop@a).
    Returns the first frame's (x, y, w, h) for the filter's starting values."""
    lines, first = [], None
    n = len(centers)
    zc = zoom if zoom is not None else np.ones(n)
    for i in range(n):
        z = float(zc[min(i, len(zc) - 1)])
        w = int(cw / z) // 2 * 2
        h = int(ch / z) // 2 * 2
        x = int(round(min(max(0.0, centers[i] - w / 2), sw - w)))
        if y_center is None:
            y = int(round((sh - h) * 0.35))             # a bit above centre, where faces usually are
        else:
            y = int(round(min(max(0.0, y_center - h * 0.42), sh - h)))
        if first is None:
            first = (x, y, w, h)
        lines.append(f"{i / fps:.3f} {target} w {w}, {target} h {h}, {target} x {x}, {target} y {y};")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return first
