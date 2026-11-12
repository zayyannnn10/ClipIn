"""Auto-framing: follow the speaker's face so a wide video can be cropped to vertical.

Faces are found 5 times a second with OpenCV's YuNet face detector (models/face_detection_yunet_2023mar.onnx,
about 230 KB; Settings can download it). The crop then moves like a calm camera operator: it ignores small
movements, pans smoothly, and jumps straight to a new position on a scene cut. The result is an FFmpeg
'sendcmd' file that moves the crop every frame. Without the model file, clips are centre-cropped."""
import math

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


def track(src, start, end, src_w, src_h, cw, on_progress):
    """Returns [(t, x)] crop positions (t from clip start, x = crop's left edge in source pixels),
    or None if no face was found anywhere (or the face model isn't installed)."""
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
        log.info("Reframe: no faces found, using the centre")
        return None

    # 1) one face per sample: the biggest, but stick with the current speaker unless another is much bigger
    raw, cur = [], None
    for t, faces in samples:
        if not faces:
            raw.append((t, None))
            continue
        faces.sort(key=lambda f: -f[2] * f[3])
        best = faces[0]
        if cur is not None and len(faces) > 1:
            near = min(faces, key=lambda f: abs(f[0] + f[2] / 2 - cur))
            if best[2] * best[3] < 1.6 * near[2] * near[3]:
                best = near
        cur = best[0] + best[2] / 2
        raw.append((t, cur))
    # 2) fill gaps (hold the last position; the start takes the first seen)
    first = next(x for _, x in raw if x is not None)
    filled, last = [], first
    for t, x in raw:
        last = x if x is not None else last
        filled.append((t, last))
    # 3) median of 5 removes single wrong detections
    xs = [x for _, x in filled]
    med = [sorted(xs[max(0, i - 2):i + 3])[len(xs[max(0, i - 2):i + 3]) // 2] for i in range(len(xs))]
    # 4) camera operator: dead zone, smooth pan, cut on big jumps
    dead, cam, pending, out = cw * 0.12, med[0], 0, []
    for (t, _), x in zip(filled, med):
        d = x - cam
        if abs(d) > src_w * 0.22:
            pending += 1
            if pending >= 2:
                cam, pending = x, 0
        else:
            pending = 0
            if abs(d) > dead:
                cam += (d - math.copysign(dead, d)) * 0.35
        out.append((t, min(max(0.0, cam - cw / 2), float(src_w - cw))))
    return out


def zoom_points(words, start, end, factor=1.14, min_len=1.6):
    """Punch-in zoom cuts: alternate normal / zoomed at sentence starts. Returns [(t, zoom)]."""
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


def write_cmds(keys, fps, dur, path, cw, sh, sw, zooms=None):
    """One crop command per frame: x follows the keys (linear between them), and with zoom cuts the crop
    gets smaller around the same centre (the scale filter after it makes that a punch-in)."""
    fps = max(1.0, min(fps, 60.0))
    lines, k, zi = [], 0, 0
    zooms = zooms or [(0.0, 1.0)]
    for i in range(int(dur * fps) + 1):
        t = i / fps
        while k + 1 < len(keys) and keys[k + 1][0] <= t:
            k += 1
        if k + 1 < len(keys) and keys[k + 1][0] > keys[k][0]:
            (t0, x0), (t1, x1) = keys[k], keys[k + 1]
            x = x0 + (x1 - x0) * max(0.0, min(1.0, (t - t0) / (t1 - t0)))
        else:
            x = keys[k][1]
        while zi + 1 < len(zooms) and zooms[zi + 1][0] <= t:
            zi += 1
        z = zooms[zi][1]
        w = int(cw / z) // 2 * 2
        h = int(sh / z) // 2 * 2
        cx = x + cw / 2
        nx = int(round(min(max(0.0, cx - w / 2), sw - w)))
        ny = int(round((sh - h) * 0.35))           # zoom a bit above centre, where faces usually are
        lines.append(f"{t:.3f} crop w {w}, crop h {h}, crop x {nx}, crop y {ny};")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return int(round(keys[0][1]))
