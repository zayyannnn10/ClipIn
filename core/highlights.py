"""Find the best moments, in two AI passes plus real viewer data:

  1. scout   the AI reads the timestamped transcript and proposes about twice as many clips as needed
  2. judge   the AI compares all candidates side by side (hook, standalone, payoff, emotion, shareability)
             and scores them again - comparing beats scoring each clip alone
  +  replay  YouTube's "Most replayed" graph (when the video has one) shows where viewers rewind;
             those moments are pointed out to the AI and raise the final score

Times are snapped to whole sentences and checked against the length the user asked for."""
import math
import re

from core import ai
from core.log import log

LENGTHS = {"15-30": (15, 30), "30-60": (30, 60), "60-90": (60, 90)}

SYSTEM = ("You are an expert short-form video editor and growth strategist. You find the moments in long videos "
          "that become viral standalone clips on TikTok, Instagram Reels and YouTube Shorts. You answer with JSON "
          "only.")

CRITERIA = """What makes a great clip:
- HOOK: the first 3 seconds stop the scroll (bold claim, question, surprising fact, conflict, strong emotion, \
something visually or verbally shocking).
- STANDALONE: makes complete sense without the rest of the video; no "as I said earlier", no missing context.
- PAYOFF: ends on a punchline, reveal, lesson, twist or strong emotional beat - not mid-thought.
- ENERGY: emotion, humour, tension, passion, a hot take or a relatable struggle.
- SHAREABLE: people would send it to a friend, comment on it, or rewatch it.
Avoid: intros and greetings, "like and subscribe", sponsor reads, outros, filler, small talk, slow setup with \
no payoff, and moments that only make sense with visuals that the words don't explain."""


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


# YouTube "Most replayed" -----------------------------------------------------------------------------

def replay(start, end, heatmap):
    """Average replay level (0-1) over a time range, or None if the video has no replay data."""
    if not heatmap:
        return None
    tot = wsum = 0.0
    for h in heatmap:
        a, b = max(start, h["start_time"]), min(end, h["end_time"])
        if b > a:
            tot += (b - a) * float(h.get("value") or 0)
            wsum += b - a
    return tot / wsum if wsum else None


def _peaks(heatmap, lo_t, hi_t, top=8):
    if not heatmap:
        return ""
    segs = sorted((h for h in heatmap if lo_t <= h["start_time"] <= hi_t), key=lambda h: -float(h.get("value") or 0))
    segs = [h for h in segs[:top] if float(h.get("value") or 0) >= 0.45]
    if not segs:
        return ""
    lines = "\n".join(f"- {h['start_time']:.0f}s to {h['end_time']:.0f}s (replay level {float(h['value']):.2f})"
                      for h in sorted(segs, key=lambda h: h["start_time"]))
    return ("\nMOST REPLAYED by real viewers (YouTube data, 1.00 = most rewatched part of the video). Strongly prefer "
            "clips that include these moments when they also meet the criteria:\n" + lines + "\n")


# pass 1: scout ---------------------------------------------------------------------------------------

def _prompt(sents, title, n, lo, hi, focus, heatmap):
    lines = "\n".join(f"[{s['s']:.1f}] {s['text']}" for s in sents)
    focus_line = f"\nThe user especially wants: {focus.strip()}. Weigh this heavily.\n" if focus and focus.strip() else ""
    return f"""Below is a timestamped transcript of a video{f' titled "{title}"' if title else ''}. \
Each line starts with its start time in seconds.

Find the {n} best moments to turn into standalone short clips.
{CRITERIA}
Rules:
- Each clip must be between {lo} and {hi} seconds long.
- Start at the beginning of a sentence or thought; end after a complete thought or punchline.
- Clips must not overlap. Spread them across the video if several parts are strong.
{_peaks(heatmap, sents[0]['s'], sents[-1]['e'])}{focus_line}
For each clip give:
- start: start time in seconds (use the line times)
- end: end time in seconds
- title: a catchy social media title, at most 8 words
- hook: one line of on-screen text for the first seconds, at most 10 words
- reason: why this moment works, one sentence
- score: 0-100, how likely it is to perform well (be strict: 90+ only for exceptional moments)
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
    return {"start": round(start, 2), "end": round(end, 2),
            "title": str(c.get("title") or "").strip()[:90] or "Untitled clip",
            "hook": str(c.get("hook") or "").strip()[:90],
            "reason": str(c.get("reason") or "").strip()[:240],
            "score": _num(c.get("score"), 50)}


def _num(v, default):
    try:
        return max(0, min(100, int(float(v))))
    except (TypeError, ValueError):
        return default


def _overlap(a, b):
    inter = min(a["end"], b["end"]) - max(a["start"], b["start"])
    return inter / max(1.0, min(a["end"] - a["start"], b["end"] - b["start"]))


# pass 2: judge ---------------------------------------------------------------------------------------

def _judge(cands, sents, title, n, focus):
    blocks = []
    for k, c in enumerate(cands, 1):
        text = " ".join(s["text"] for s in sents if s["e"] > c["start"] and s["s"] < c["end"])
        rp = c.get("replay")
        blocks.append(f"CLIP {k} ({c['end'] - c['start']:.0f}s{f', replay level {rp:.2f}' if rp is not None else ''}):"
                      f"\n{text[:1600]}")
    focus_line = f"\nThe user especially wants: {focus.strip()}.\n" if focus and focus.strip() else ""
    prompt = f"""You are choosing which clips from the video{f' "{title}"' if title else ''} to post as short \
videos. Here are {len(cands)} candidate clips with what is said in each.
{CRITERIA}
{focus_line}
Compare them against each other and score every clip 0-100 for how well it would perform as a standalone short \
(be strict and spread the scores; only exceptional clips get 90+). "replay level" is real YouTube data on how much \
viewers rewatched that part (1.00 = most); treat a high level as strong evidence. Also improve the title (max 8 \
words) and the on-screen hook (max 10 words) if you can, in the clip's language.

Reply as JSON: {{"clips": [{{"clip": 1, "score": 0, "title": "", "hook": "", "reason": ""}}]}}

{chr(10).join(blocks)}"""
    data, prov, model = ai.ask(prompt, SYSTEM)
    rows = data.get("clips") if isinstance(data, dict) else data
    judged = 0
    for r in rows or []:
        try:
            c = cands[int(r.get("clip")) - 1]
        except (TypeError, ValueError, IndexError):
            continue
        c["score"] = _num(r.get("score"), c["score"])
        for key in ("title", "hook", "reason"):
            if str(r.get(key) or "").strip():
                c[key] = str(r[key]).strip()[:90 if key != "reason" else 240]
        judged += 1
    log.info("Judge scored %d of %d candidates (%s)", judged, len(cands), model)


def find(transcript, title, n, length_key, focus, on_progress, heatmap=None):
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
    heatmap = [h for h in (heatmap or []) if "start_time" in h and "end_time" in h]
    groups = _windows(sents, ai.WINDOW[plist[0]])
    want = max(n * 2, n + 4)                           # scout for more, keep the best
    per = want if len(groups) == 1 else max(3, math.ceil(want * 1.3 / len(groups)))
    cands, used = [], ""
    for k, g in enumerate(groups):
        on_progress(k / (len(groups) + 1), f"AI is reading part {k + 1} of {len(groups)}..." if len(groups) > 1
                    else "AI is reading the transcript...")
        data, prov, model = ai.ask(_prompt(g, title, per, lo, hi, focus, heatmap), SYSTEM)
        used = f"{ai.NAMES[prov]} ({model})"
        clips = data.get("clips") if isinstance(data, dict) else data
        for c in clips or []:
            if isinstance(c, dict):
                fixed = _snap(c, sents, lo, hi)
                if fixed:
                    cands.append(fixed)
    if not cands:
        raise RuntimeError("The AI didn't suggest any usable clips. Try a different length or video.")
    # drop near-duplicates before judging (keep the higher first-pass score)
    uniq = []
    for c in sorted(cands, key=lambda c: -c["score"]):
        if all(_overlap(c, u) < 0.5 for u in uniq):
            uniq.append(c)
    uniq = uniq[:max(want, 8)]
    for c in uniq:
        c["replay"] = replay(c["start"], c["end"], heatmap)
    on_progress(len(groups) / (len(groups) + 1), f"AI is comparing {len(uniq)} possible clips...")
    try:
        _judge(uniq, sents, title, n, focus)
    except Exception as e:                             # the first pass is still a good answer
        log.warning("judge pass failed, using first-pass scores: %s", e)
    for c in uniq:                                     # real viewer data nudges the final score
        if c.get("replay") is not None:
            c["score"] = int(round(0.8 * c["score"] + 0.2 * c["replay"] * 100))
    kept = []
    for c in sorted(uniq, key=lambda c: -c["score"]):
        if all(_overlap(c, k) < 0.3 for k in kept):
            kept.append(c)
        if len(kept) >= n:
            break
    kept.sort(key=lambda c: -c["score"])
    for i, c in enumerate(kept, 1):
        c["id"] = f"c{i:02d}"
        c.pop("replay", None)
    log.info("Highlights: %d candidates, %d kept (%s, replay data: %s)", len(cands), len(kept), used, bool(heatmap))
    on_progress(1.0, f"Picked the best {len(kept)} of {len(uniq)} moments with {used}"
                + (" + YouTube replay data" if heatmap else ""))
    return kept, used
