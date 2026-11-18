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


# gameplay streams: the streamer's webcam ------------------------------------------------------------

def find_facecam(samples, sw, sh):
    """A streamer's webcam on a gameplay video: a small face that stays in one place for most of the clip
    (faces in the game itself move around). Returns (cx, cy, fw, fh) in source pixels, or None when the
    video isn't a gameplay-with-webcam shot (no face, or a big face filling the picture)."""
    if not samples:
        return None
    faces = [(t, f) for t, fs in samples for f in fs]
    if not faces:
        return None
    small = [(t, f) for t, f in faces if f[3] < 0.33 * sh]          # a webcam face is small in the frame
    if not small:
        return None
    tol_x, tol_y = 0.06 * sw, 0.08 * sh
    best, best_n = None, 0
    for _, f in small:                                              # the spot most samples have a face at
        cx, cy = f[0] + f[2] / 2, f[1] + f[3] / 2
        hit = {t for t, g in small if abs(g[0] + g[2] / 2 - cx) < tol_x and abs(g[1] + g[3] / 2 - cy) < tol_y}
        if len(hit) > best_n:
            best, best_n = (cx, cy), len(hit)
    if best is None or best_n < 0.45 * len(samples):                # not there most of the time: not a webcam
        return None
    near = [f for _, f in small if abs(f[0] + f[2] / 2 - best[0]) < tol_x and abs(f[1] + f[3] / 2 - best[1]) < tol_y]
    cx = float(np.median([f[0] + f[2] / 2 for f in near]))
    cy = float(np.median([f[1] + f[3] / 2 for f in near]))
    fw = float(np.median([f[2] for f in near]))
    fh = float(np.median([f[3] for f in near]))
    big = [f for _, f in faces if f[3] >= 0.33 * sh]
    if len(big) > 0.3 * len(samples):                               # a big face most of the time: not gameplay
        return None
    log.info("Webcam found at %.0f,%.0f (face %.0fx%.0f) in %d of %d samples", cx, cy, fw, fh, best_n, len(samples))
    return cx, cy, fw, fh


def facecam_crop(cam, sw, sh, out_w, out_h):
    """Crop (x, y, w, h) around the webcam for a panel of out_w x out_h: the face a little above the middle,
    with some of the webcam around it."""
    cx, cy, fw, fh = cam
    w = min(max(fw * 3.4, sw * 0.16), sw * 0.6)
    h = w * out_h / out_w
    if h > sh:
        h = sh
        w = h * out_w / out_h
    x = min(max(cx - w / 2, 0), sw - w)
    y = min(max(cy + fh * 0.1 - h / 2, 0), sh - h)
    return int(x) // 2 * 2, int(y) // 2 * 2, int(w) // 2 * 2, int(h) // 2 * 2


def game_crop(cam_box, sw, sh, out_w, out_h):
    """Crop (x, y, w, h) of the game for a panel of out_w x out_h: the middle of the picture (where the action
    is), moved a little away from the webcam when the webcam would be in it."""
    a = out_w / out_h
    h = sh
    w = h * a
    if w > sw:
        w = sw
        h = w / a
    x = (sw - w) / 2
    if cam_box:
        cx0, _, cw_, _ = cam_box
        cam_l, cam_r = cx0, cx0 + cw_
        if cam_l < x + w and cam_r > x:                             # the webcam is inside the game crop
            limit = sw * 0.22                                       # never move far from the action
            if (cam_l + cam_r) / 2 < sw / 2:
                x = min(x + min(limit, cam_r - x), sw - w)
            else:
                x = max(x - min(limit, x + w - cam_l), 0)
    y = (sh - h) / 2
    return int(x) // 2 * 2, int(y) // 2 * 2, int(w) // 2 * 2, int(h) // 2 * 2


def sample_frames(src, start, end, sw, sh, n=12, width=640):
    """A few small grey frames spread over the clip (for finding the webcam's edges and black bars)."""
    import cv2
    cap = cv2.VideoCapture(str(src))
    if not cap.isOpened():
        return [], 1.0
    size = (width, max(2, int(sh * width / sw)))
    out = []
    for i in range(n):
        cap.set(cv2.CAP_PROP_POS_MSEC, (start + (i + 0.5) * (end - start) / n) * 1000)
        ok, fr = cap.read()
        if ok:
            out.append(cv2.cvtColor(cv2.resize(fr, size), cv2.COLOR_BGR2GRAY).astype(np.int16))
    cap.release()
    return out, sw / width


def _edge(score, lo, hi, prefer_far=False):
    """Index of the strongest steady edge between lo and hi, or None when no clear edge is there."""
    lo, hi = max(0, int(lo)), min(len(score), int(hi))
    if hi - lo < 3:
        return None
    seg = score[lo:hi]
    i = int(np.argmax(seg))
    if seg[i] < 10 or seg[i] < 2.2 * float(np.median(seg)):
        return None
    return lo + i


def cam_rect(frames, scale, cam, sw, sh):
    """The webcam's box (x0, y0, x1, y1) in source pixels, from its border lines: they stay put while the game
    moves, so they stand out in the median edge strength. Missing edges are guessed from the face size."""
    cx, cy, fw, fh = (v / scale for v in cam)
    if not frames:
        return _guess_rect(cam, sw, sh), 0
    h, w = frames[0].shape
    rows = slice(max(0, int(cy - fh)), min(h, int(cy + fh)))
    cols = slice(max(0, int(cx - fw)), min(w, int(cx + fw)))
    colscore = np.median([np.abs(np.diff(f[rows], axis=1)).mean(axis=0) for f in frames], axis=0)
    rowscore = np.median([np.abs(np.diff(f[:, cols], axis=0)).mean(axis=1) for f in frames], axis=0)
    g = _guess_rect(cam, sw, sh)
    near_l, near_r = cx - fw * 0.9, cx + fw * 0.9
    near_t, near_b = cy - fh * 0.9, cy + fh * 0.9
    r = _edge(colscore, near_r, cx + fw * 10)
    l = _edge(colscore, cx - fw * 10, near_l)
    b = _edge(rowscore, near_b, cy + fh * 8)
    t = _edge(rowscore, cy - fh * 8, near_t)
    x0 = (l + 1) * scale if l is not None else (0 if cx - fw * 10 <= 0 else g[0])
    x1 = (r + 1) * scale if r is not None else (sw if cx + fw * 10 >= w else g[2])
    y0 = (t + 1) * scale if t is not None else (0 if cy - fh * 8 <= 0 else g[1])
    y1 = (b + 1) * scale if b is not None else (sh if cy + fh * 8 >= h else g[3])
    pad = 3 * scale                                   # stay a little inside a found border (no game sliver)
    x0 += pad if l is not None else 0
    x1 -= pad if r is not None else 0
    y0 += pad if t is not None else 0
    y1 -= pad if b is not None else 0
    found = sum(e is not None for e in (l, r, t, b))
    log.info("Webcam box %.0f,%.0f to %.0f,%.0f (%d border lines found)", x0, y0, x1, y1, found)
    return (x0, y0, x1, y1), found


def _guess_rect(cam, sw, sh):
    cx, cy, fw, fh = cam
    return max(0, cx - fw * 4), max(0, cy - fh * 3), min(sw, cx + fw * 4), min(sh, cy + fh * 3)


def crop_in_rect(rect, cam, out_w, out_h, sw):
    """The biggest out_w:out_h crop inside the webcam box, centred on the face (top panel)."""
    x0, y0, x1, y1 = rect
    cx, cy, fw, fh = cam
    rw, rh = x1 - x0, y1 - y0
    a = out_w / out_h
    if rw / max(rh, 1) > a:
        h = rh
        w = h * a
    else:
        w = rw
        h = w / a
    tight = max(fw * 4.5, sw * 0.16)            # keep the face big and in the middle...
    if w > tight:
        w, h = tight, tight / a                      # ...but never show anything outside the webcam
    x = min(max(cx - w / 2, x0), x1 - w)
    y = min(max(cy + fh * 0.1 - h / 2, y0), y1 - h)
    return int(x) // 2 * 2, int(y) // 2 * 2, max(2, int(w) // 2 * 2), max(2, int(h) // 2 * 2)


def black_bars(frames, scale, sh, x0=0, x1=None):
    """Rows (top, bottom) of black bars the stream itself has above and below the game, in source pixels."""
    if not frames:
        return 0, 0
    w = frames[0].shape[1]
    lo, hi = int(x0 / scale), int((x1 or w * scale) / scale)
    rowmax = np.max([f[:, lo:hi].mean(axis=1) for f in frames], axis=0)   # black in every frame
    top = 0
    while top < len(rowmax) and rowmax[top] < 18:
        top += 1
    bot = 0
    while bot < len(rowmax) - top and rowmax[len(rowmax) - 1 - bot] < 18:
        bot += 1
    if (top + bot) > 0.4 * len(rowmax):
        return 0, 0
    return top * scale, bot * scale


def game_crop2(rect, sw, sh, out_w, out_h, bars=(0, 0)):
    """The game panel: the middle of the picture (where the action is) without the stream's black bars,
    moved out of the webcam box when the webcam would show in it (never more than 30% of the width)."""
    a = out_w / out_h
    top, bot = bars
    y0, avail = top, sh - top - bot
    h = avail
    w = h * a
    if w > sw:
        w = sw
        h = w / a
    x = (sw - w) / 2
    if rect:
        cl, ct, cr, cb = rect
        overlaps_rows = ct < y0 + h and cb > y0
        if overlaps_rows and cl < x + w and cr > x:
            limit = sw * 0.30
            if (cl + cr) / 2 < sw / 2:
                x = min(x + min(limit, cr - x), sw - w)
            else:
                x = max(x - min(limit, x + w - cl), 0)
    y = y0 + (avail - h) / 2
    return int(x) // 2 * 2, int(y) // 2 * 2, int(w) // 2 * 2, int(h) // 2 * 2
