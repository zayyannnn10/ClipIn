"""Find the best moments: the AI reads the timestamped transcript and proposes clips; the times are then
snapped to whole sentences and checked against the length the user asked for."""
import math
import re

from core import ai
from core.log import log

LENGTHS = {"15-30": (15, 30), "30-60": (30, 60), "60-90": (60, 90)}

SYSTEM = ("You are an expert short-form video editor. You find the moments in long videos that work as "
          "standalone viral clips on TikTok, Instagram Reels and YouTube Shorts. You answer with JSON only.")


def sentences(words, max_words=28):
    """Group words into sentences (split on . ! ? or a pause)."""
    out, cur = [], []

    def flush():
        if cur:
            out.append({"s": cur[0]["s"], "e": cur[-1]["e"], "text": " ".join(w["w"] for w in cur)})
            cur.clear()
    for w in words:
        if cur and w["s"] - cur[-1]["e"] > 0.9:
            flush()
        cur.append(w)
        if re.search(r"[.!?…。؟]$", w["w"]) or len(cur) >= max_words:
            flush()
    flush()
    return out


def _prompt(sents, title, n, lo, hi, focus):
    lines = "\n".join(f"[{s['s']:.1f}] {s['text']}" for s in sents)
    focus_line = f"- The user especially wants: {focus.strip()}\n" if focus and focus.strip() else ""
    return f"""Below is a timestamped transcript of a video{f' titled "{title}"' if title else ''}. \
Each line starts with its start time in seconds.

Find the {n} best moments to turn into standalone short clips.
Rules:
- Each clip must be between {lo} and {hi} seconds long.
- A clip must make sense on its own: start at the beginning of a sentence or thought and end after a \
complete thought or punchline. Never cut mid-sentence.
- The first 3 seconds must hook the viewer: a bold claim, a question, a surprising fact, strong emotion or conflict.
- Prefer moments with a clear payoff: a story, an insight, a funny moment, a strong opinion or a useful tip.
- Clips must not overlap.
{focus_line}
For each clip give:
- start: start time in seconds (use the line times)
- end: end time in seconds
- title: a catchy social media title, at most 8 words
- hook: one line of on-screen text for the first seconds, at most 10 words
- reason: why this moment works, one sentence
- score: 0-100, how likely it is to perform well
Write title and hook in the same language as the transcript.

Reply as JSON: {{"clips": [{{"start": 0.0, "end": 0.0, "title": "", "hook": "", "reason": "", "score": 0}}]}}

TRANSCRIPT:
{lines}"""


def _windows(sents, size, overlap=45):
    if not sents:
        return []
    total = sents[-1]["e"]
    if total <= size * 1.15:
        return [sents]
    out, t = [], 0.0
    while t < total:
        part = [s for s in sents if s["s"] >= t - overlap and s["s"] < t + size]
        if part:
            out.append(part)
        t += size
    return out


def _snap(c, sents, lo, hi):
    try:
        s, e = float(c["start"]), float(c["end"])
    except (KeyError, TypeError, ValueError):
        return None
    if e <= s:
        return None
    i = min(range(len(sents)), key=lambda k: abs(sents[k]["s"] - s))
    j = min(range(i, len(sents)), key=lambda k: abs(sents[k]["e"] - e))
    while j > i and sents[j]["e"] - sents[i]["s"] > hi + 3:
        j -= 1
    while j < len(sents) - 1 and sents[j]["e"] - sents[i]["s"] < lo:
        j += 1
    start = max(0.0, sents[i]["s"] - 0.15)
    end = sents[j]["e"] + 0.35
    if end - start > hi + 8:                          # one very long sentence: cut at the limit
        end = start + hi
    if end - start < lo * 0.6:
        return None
    score = c.get("score", 50)
    try:
        score = max(0, min(100, int(float(score))))
    except (TypeError, ValueError):
        score = 50
    return {"start": round(start, 2), "end": round(end, 2),
            "title": str(c.get("title") or "").strip()[:90] or "Untitled clip",
            "hook": str(c.get("hook") or "").strip()[:90],
            "reason": str(c.get("reason") or "").strip()[:240],
            "score": score}


def _overlap(a, b):
    inter = min(a["end"], b["end"]) - max(a["start"], b["start"])
    return inter / max(1.0, min(a["end"] - a["start"], b["end"] - b["start"]))


def find(transcript, title, n, length_key, focus, on_progress):
    lo, hi = LENGTHS.get(length_key, (30, 60))
    sents = sentences(transcript.get("words") or [])
    if not sents:
        sents = [{"s": s["s"], "e": s["e"], "text": s["text"]} for s in transcript.get("segments") or []
                 if s.get("text")]
    if not sents:
        raise RuntimeError("No speech was found in this video, so there's nothing to clip.")
    plist = ai.providers()
    if not plist:
        raise RuntimeError("No AI is set up. Add a Gemini or Groq key in Settings, or choose Ollama.")
    groups = _windows(sents, ai.WINDOW[plist[0]])
    per = n if len(groups) == 1 else max(2, math.ceil(n * 1.5 / len(groups)))
    cands, used = [], ""
    for k, g in enumerate(groups):
        on_progress(k / len(groups), f"AI is reading part {k + 1} of {len(groups)}..." if len(groups) > 1
                    else "AI is reading the transcript...")
        data, prov, model = ai.ask(_prompt(g, title, per, lo, hi, focus), SYSTEM)
        used = f"{ai.NAMES[prov]} ({model})"
        clips = data.get("clips") if isinstance(data, dict) else data
        for c in clips or []:
            if isinstance(c, dict):
                fixed = _snap(c, sents, lo, hi)
                if fixed:
                    cands.append(fixed)
    log.info("Highlights: %d candidates from %s", len(cands), used)
    if not cands:
        raise RuntimeError("The AI didn't suggest any usable clips. Try a different length or video.")
    kept = []
    for c in sorted(cands, key=lambda c: -c["score"]):
        if all(_overlap(c, k) < 0.3 for k in kept):
            kept.append(c)
        if len(kept) >= n:
            break
    for i, c in enumerate(kept, 1):
        c["id"] = f"c{i:02d}"
    on_progress(1.0, f"Found {len(kept)} moments with {used}")
    return kept, used
