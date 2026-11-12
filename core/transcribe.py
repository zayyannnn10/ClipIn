"""Speech to text with word timestamps.

Engines:
  local_gpu  faster-whisper on the NVIDIA GPU (needs cuBLAS 12 + cuDNN 9 DLLs:
             pip install nvidia-cublas-cu12 nvidia-cudnn-cu12)
  local_cpu  faster-whisper on the processor (slower, always works)
  groq       Groq's hosted Whisper (free tier, very fast, needs a Groq key)
  auto       GPU if ready, else Groq if there's a key, else CPU
"""
import ctypes
import gc
import json
import math
import os
import site
import time
from pathlib import Path

import requests

from core import media
from core.config import cfg, key
from core.log import log
from core.project import fmt_time

GROQ = "https://api.groq.com/openai/v1"


def _cuda_dirs():
    bases = list(site.getsitepackages()) + [site.getusersitepackages()]
    out = []
    for b in bases:
        nv = Path(b) / "nvidia"
        if nv.is_dir():
            out += [p for p in nv.glob("*/bin") if p.is_dir()]
    return out


def add_cuda_dlls():
    for d in _cuda_dirs():
        try:
            os.add_dll_directory(str(d))
        except OSError:
            pass
        if str(d) not in os.environ.get("PATH", ""):
            os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")


def cuda_status():
    """(ok, message) - can Whisper run on the GPU?"""
    if os.name != "nt":
        return False, "GPU Whisper is only set up for Windows"
    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() < 1:
            return False, "No NVIDIA GPU found"
    except Exception as e:
        return False, f"CTranslate2 problem: {e}"
    add_cuda_dlls()
    missing = []
    for dll in ("cublas64_12.dll", "cudnn64_9.dll"):
        try:
            ctypes.WinDLL(dll)
        except OSError:
            missing.append(dll)
    if missing:
        return False, "Missing CUDA libraries: " + ", ".join(missing)
    return True, "GPU ready"


def pick_engine():
    e = cfg.get("transcribe.engine", "auto")
    if e != "auto":
        return e
    if cuda_status()[0]:
        return "local_gpu"
    if key("groq_api_key"):
        return "groq"
    return "local_cpu"


def transcribe(src, workdir: Path, duration, language, on_progress):
    """Returns {'language', 'engine', 'words': [{w, s, e}], 'segments': [{s, e, text}]}; cached on disk."""
    out = workdir / "transcript.json"
    if out.exists():
        return json.loads(out.read_text(encoding="utf-8"))
    lang = None if language in (None, "", "auto") else language
    engine = pick_engine()
    log.info("Transcribing with %s", engine)
    res = None
    if engine == "groq":
        try:
            res = _groq(src, workdir, duration, lang, on_progress)
        except Exception as e:
            log.warning("Groq transcription failed: %s", e)
            on_progress(0.0, "Groq didn't work, switching to local Whisper...")
            engine = "local_gpu" if cuda_status()[0] else "local_cpu"
    if res is None:
        res = _local(src, workdir, duration, lang, on_progress, gpu=engine == "local_gpu")
        engine = res.pop("_engine", engine)
    res["engine"] = engine
    out.write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
    return res


# local Whisper ------------------------------------------------------------------------------------

def _local(src, workdir, duration, lang, on_progress, gpu):
    import numpy as np
    from faster_whisper import WhisperModel

    raw = workdir / "audio16k.raw"
    if not raw.exists():
        on_progress(0.0, "Extracting audio...")
        tmp = workdir / "audio16k.tmp"
        media.run(["-i", src, "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", tmp])
        tmp.replace(raw)
    # (the audio is passed as an array: faster-whisper's own file reader breaks with newer PyAV)
    audio = np.fromfile(raw, dtype=np.int16).astype(np.float32) / 32768.0
    name = cfg.get("transcribe.model", "small")
    if gpu:
        add_cuda_dlls()
    on_progress(0.0, f"Loading Whisper ({name}, {'GPU' if gpu else 'CPU'})...")
    model = None
    try:
        model = WhisperModel(name, device="cuda" if gpu else "cpu",
                             compute_type="int8_float16" if gpu else "int8",
                             cpu_threads=max(2, (os.cpu_count() or 4) - 2))
        segs, info = model.transcribe(audio, language=lang, word_timestamps=True, vad_filter=True,
                                      beam_size=5 if gpu else 2, condition_on_previous_text=False)
        words, segments = [], []
        for s in segs:
            for w in s.words or []:
                if w.word.strip():
                    words.append({"w": w.word.strip(), "s": round(w.start, 3), "e": round(w.end, 3)})
            segments.append({"s": round(s.start, 3), "e": round(s.end, 3), "text": s.text.strip()})
            on_progress(min(0.99, s.end / max(duration, 1)),
                        f"Transcribed {fmt_time(s.end)} of {fmt_time(duration)}")
        res = {"language": info.language, "words": words, "segments": segments,
               "_engine": "local_gpu" if gpu else "local_cpu"}
    except RuntimeError as e:
        if gpu and any(k in str(e).lower() for k in ("cublas", "cudnn", "cuda")):
            log.warning("GPU Whisper failed (%s), using the CPU", e)
            del model
            gc.collect()
            return _local(src, workdir, duration, lang, on_progress, gpu=False)
        raise
    del model, audio
    gc.collect()
    try:
        raw.unlink()
    except OSError:
        pass
    return res


# Groq Whisper -------------------------------------------------------------------------------------

def _groq_model(k):
    want = cfg.get("transcribe.groq_model") or ""
    if want:
        return want
    try:
        r = requests.get(f"{GROQ}/models", headers={"Authorization": f"Bearer {k}"}, timeout=15)
        if r.status_code in (401, 403):
            raise RuntimeError("Groq rejected the API key")
        ids = [m["id"] for m in r.json().get("data", [])]
        for m in ("whisper-large-v3-turbo", "whisper-large-v3"):
            if m in ids:
                return m
    except requests.RequestException:
        pass
    return "whisper-large-v3-turbo"


def _groq(src, workdir, duration, lang, on_progress):
    k = key("groq_api_key")
    if not k:
        raise RuntimeError("no Groq key")
    model = _groq_model(k)
    chunk = 600                                  # 10-minute pieces stay far below Groq's file limit
    n = max(1, math.ceil((duration or 1) / chunk))
    words, segments, language = [], [], ""
    for i in range(n):
        start = i * chunk
        part = workdir / f"groq_{i:03d}.mp3"
        on_progress(i / n, f"Sending part {i + 1} of {n} to Groq Whisper...")
        media.run(["-ss", start, "-t", chunk, "-i", src, "-vn", "-ac", "1", "-ar", "16000", "-b:a", "48k", part])
        data = [("model", model), ("response_format", "verbose_json"),
                ("timestamp_granularities[]", "word"), ("timestamp_granularities[]", "segment")]
        if lang:
            data.append(("language", lang))
        r = None
        for _attempt in range(5):
            with open(part, "rb") as f:
                r = requests.post(f"{GROQ}/audio/transcriptions", headers={"Authorization": f"Bearer {k}"},
                                  data=data, files={"file": (part.name, f, "audio/mpeg")}, timeout=300)
            if r.status_code == 429:
                wait = float(r.headers.get("retry-after") or 20)
                on_progress(i / n, f"Groq asks to wait {wait:.0f}s (free-tier limit)...")
                time.sleep(min(wait, 120))
                continue
            break
        try:
            part.unlink()
        except OSError:
            pass
        if r is None or r.status_code >= 400:
            raise RuntimeError(f"Groq HTTP {r.status_code if r is not None else '?'}: "
                               f"{(r.text if r is not None else '')[:200]}")
        d = r.json()
        language = language or d.get("language") or ""
        for w in d.get("words") or []:
            if str(w.get("word", "")).strip():
                words.append({"w": w["word"].strip(), "s": round(w["start"] + start, 3),
                              "e": round(w["end"] + start, 3)})
        for s in d.get("segments") or []:
            segments.append({"s": round(s["start"] + start, 3), "e": round(s["end"] + start, 3),
                             "text": str(s.get("text", "")).strip()})
        on_progress((i + 1) / n, f"Transcribed {fmt_time(min(duration, start + chunk))} of {fmt_time(duration)}")
    return {"language": language, "words": words, "segments": segments}
