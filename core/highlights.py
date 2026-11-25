"""Find the best moments, in two AI passes plus real viewer data:

  1. scout   the AI reads the timestamped transcript and proposes about twice as many clips as needed
  2. judge   the AI compares all candidates side by side (hook, standalone, payoff, emotion, shareability)
             and scores them again - comparing beats scoring each clip alone; the first 2 seconds get a
             score of their own, because that's where Shorts viewers swipe away
  +  tease   each clip may carry a 1-3 s "cold open": its best moment, played first (see core/render.py)
  +  niche   with a channel niche set, moments are picked and written for that audience
  +  replay  YouTube's "Most replayed" graph (when the video has one) shows where viewers rewind;
             those moments are pointed out to the AI and raise the final score

Times are snapped to whole sentences and checked against the length the user asked for."""
import math
import re

from core import ai
from core.log import log

LENGTHS = {"auto": (15, 45), "15-30": (15, 30), "30-60": (30, 60), "60-90": (60, 90)}
TEASE_MIN, TEASE_MAX = 1.0, 3.5                         # hook-first opening: seconds of the best moment shown first

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
- TIGHT: ends right after the payoff or reaction. Cut as soon as the topic changes or the energy drops; a \
clip that drifts into unrelated talk loses the viewer. Never pad a clip to reach a length.
Avoid: intros and greetings, "like and subscribe", sponsor reads, outros, filler, small talk, slow setup with \
no payoff, and moments that only make sense with visuals that the words don't explain."""

HOOK_RULES = """On-screen hook text (shown over the first seconds):
- 3 to 7 words that open a question the clip answers: tease the situation, never give away the payoff.
- Never repeat what is being said at that moment (viewers hear it already), never describe the ending.
- Plain, punchy, specific. Capitals on at most one word. No hashtags. Emoji only if it truly adds.
- Good: "He did NOT expect this", "Chat set him up", "Why is the plane upside down?".
- Bad: "BIGFOOT is real in GTA 6" (spoils it), "Is that Scooby-Doo driving a car?" (repeats the line),
  "Finally, a smooth landing!" (no question to answer)."""

TEASE_RULE = """- tease_start / tease_end / tease_quote: the single most gripping 1 to 3 seconds INSIDE the clip \
(the scream, the punchline, the shocking line, the big reaction). It is played first, before the clip starts, as \
a cold open, so it must grab attention on its own and make sense without context, without spoiling the whole \
ending. tease_quote is its exact words copied from the transcript (2 to 10 words, a complete phrase - never \
stop mid-phrase). It must start at least 3 seconds after the clip's start. Use null for all three if the clip \
already opens with its strongest moment or no moment works on its own."""


def _niche_line(niche):
    return (f"\nThis is for a channel whose niche is: {niche.strip()}. Pick moments this audience cares about, "
            f"write titles and hooks for them, and score moments outside the niche lower.\n"
            if niche and niche.strip() else "")


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

def _prompt(sents, title, n, lo, hi, focus, heatmap, niche="", tight=False):
    lines = "\n".join(f"[{s['s']:.1f}-{s['e']:.1f}] {s['text']}" for s in sents)
    focus_line = f"\nThe user especially wants: {focus.strip()}. Weigh this heavily.\n" if focus and focus.strip() else ""
    length_rule = (f"- Each clip is {lo} to {hi} seconds: use the SHORTEST length that holds the whole moment "
                   f"(most strong moments fit in 15-35 seconds)." if tight else
                   f"- Each clip must be between {lo} and {hi} seconds long.")
    return f"""Below is a timestamped transcript of a video{f' titled "{title}"' if title else ''}. \
Each line starts with its start and end time in seconds.

Find the {n} best moments to turn into standalone short clips.
{CRITERIA}
Rules:
{length_rule}
- Start as late as possible: at the first line the viewer needs, ideally one that already grabs attention. \
End right after the payoff.
- Clips must not overlap. Spread them across the video if several parts are strong.
{_peaks(heatmap, sents[0]['s'], sents[-1]['e'])}{focus_line}{_niche_line(niche)}
{HOOK_RULES}

For each clip give:
- start: start time in seconds (the start time of the first line to include)
- end: end time in seconds (the END time of the last line to include)
{TEASE_RULE}
- title: a catchy social media title, at most 8 words
- hook: the on-screen hook text (rules above)
- thumb: 2 to 4 punchy words for the thumbnail picture (no hashtags, no emoji)
- reason: why this moment works, one sentence
- score: 0-100, how likely it is to perform well (be strict: 90+ only for exceptional moments)
Write title and hook in the same language as the transcript.

Reply as JSON: {{"clips": [{{"start": 0.0, "end": 0.0, "tease_start": null, "tease_end": null, "tease_quote": null, \
"title": "", \
"hook": "", "thumb": "", "reason": "", "score": 0}}]}}

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


def _snap(c, sents, lo, hi, words=None):
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
    out = {"start": round(start, 2), "end": round(end, 2),
           "title": str(c.get("title") or "").strip()[:90] or "Untitled clip",
           "hook": str(c.get("hook") or "").strip()[:90],
           "thumb_text": str(c.get("thumb") or "").strip()[:40],
           "reason": str(c.get("reason") or "").strip()[:240],
           "score": _num(c.get("score"), 50)}
    tease = _tease(c, out["start"], out["end"], words or [], sents)
    if tease:
        out["tease"] = tease
    return out


def _norm(w):
    return re.sub(r"[^\w']", "", str(w).lower())


def _quote_span(quote, words, near, start, end):
    """Find the AI's quoted teaser words in the clip's word timings (the closest good match to `near`).
    Returns (first index, last index) or None."""
    q = [x for x in (_norm(t) for t in str(quote or "").split()) if x][:14]
    if not q:
        return None
    idx = [k for k, w in enumerate(words) if w["s"] >= start - 0.05 and w["e"] <= end + 0.05]
    n = len(q)
    best = None
    for pos in range(len(idx)):
        window = [_norm(words[k]["w"]) for k in idx[pos:pos + n]]
        hits = sum(1 for a, b in zip(q, window) if a == b)
        if hits < max(1, round(n * 0.6)):
            continue
        k0 = idx[pos]
        key = (-hits, abs(words[k0]["s"] - near) if near is not None else 0)
        if best is None or key < best[0]:
            best = (key, k0, idx[min(pos + n, len(idx)) - 1])
    return (best[1], best[2]) if best else None


def _tease(c, start, end, words, sents):
    """The AI's cold-open moment, matched to the exact quoted words when it gives them, kept 1-3.5 s long,
    ending on a natural pause, and inside the clip (not in its first seconds). Returns {"start", "end"} or None."""
    def num(k):
        try:
            return float(c.get(k))
        except (TypeError, ValueError):
            return None
    ts, te = num("tease_start"), num("tease_end")
    lo_t = start + 2.5                                 # at the very start a teaser adds nothing
    span = _quote_span(c.get("tease_quote"), words, ts, lo_t, end) if words else None
    if span:
        k0, k1 = span
    elif ts is not None and words and lo_t <= ts < end - 0.5:
        ks = [k for k, w in enumerate(words) if w["e"] > ts - 0.05 and w["s"] < end]
        if not ks:
            return None
        k0 = ks[0]
        stop = te if te and te > ts else ts + 2.5
        k1 = max([k for k in ks if words[k]["s"] < stop + 0.05] or [k0])
    elif ts is not None and lo_t <= ts < end - 0.5:    # no word times: the sentence it falls in
        sent = next((x for x in sents if x["s"] <= ts < x["e"]), None)
        if not sent:
            return None
        a, b = sent["s"] - 0.08, min(sent["e"] + 0.2, sent["s"] + TEASE_MAX)
        return {"start": round(a, 2), "end": round(b, 2)} if b - a >= TEASE_MIN * 0.8 else None
    else:
        return None
    a = words[k0]["s"]
    if a < lo_t or a >= end - 0.5:
        return None
    last = len(words) - 1
    while words[k1]["e"] - a < TEASE_MIN and k1 < last and words[k1 + 1]["e"] <= end:   # too short: go on
        k1 += 1
    cut = False
    while k1 > k0 and words[k1]["e"] - a > TEASE_MAX:  # too long: drop words from the end
        k1 -= 1
        cut = True

    def brk(k):                                        # a natural break after word k: pause or punctuation
        gap = (words[k + 1]["s"] - words[k]["e"]) if k < last else 1.0
        return gap >= 0.28 or str(words[k]["w"]).rstrip().endswith((".", "!", "?", ","))
    if cut or not span:                                # the end is a guess: move it back to a natural break
        k = k1
        while k > k0 and not brk(k) and words[k]["e"] - a > TEASE_MIN:
            k -= 1
        if brk(k):
            k1 = k
    b = words[k1]["e"] + 0.18
    if b - a < TEASE_MIN * 0.8:
        return None
    return {"start": round(max(a - 0.08, start + 2.0), 2), "end": round(min(b, end), 2)}

def _num(v, default):
    try:
        return max(0, min(100, int(float(v))))
    except (TypeError, ValueError):
        return default


def _overlap(a, b):
    inter = min(a["end"], b["end"]) - max(a["start"], b["start"])
    return inter / max(1.0, min(a["end"] - a["start"], b["end"] - b["start"]))


# pass 2: judge ---------------------------------------------------------------------------------------

def _said(sents, a, b):
    return " ".join(s["text"] for s in sents if s["e"] > a and s["s"] < b)


def _judge(cands, sents, title, n, focus, prov=None, niche=""):
    blocks = []
    per_clip = {"groq": 380, "omniroute": 700}.get(prov, 1600)   # small free limits: shorter excerpts
    for k, c in enumerate(cands, 1):
        text = _said(sents, c["start"], c["end"])
        rp = c.get("replay")
        t = c.get("tease")
        first = (f"TEASER played first: \"{_said(sents, t['start'], t['end'])[:160]}\"" if t else
                 f"OPENS WITH: \"{_said(sents, c['start'], c['start'] + 3)[:160]}\"")
        blocks.append(f"CLIP {k} ({c['end'] - c['start']:.0f}s{f', replay level {rp:.2f}' if rp is not None else ''}):"
                      f"\n{first}\nHOOK TEXT: \"{c.get('hook', '')}\"\n{text[:per_clip]}")
    focus_line = f"\nThe user especially wants: {focus.strip()}.\n" if focus and focus.strip() else ""
    fit_ask = ', "fit": 0' if niche and niche.strip() else ""
    fit_rule = ("\n- fit: 0-100, how well the clip fits the channel's niche." if fit_ask else "")
    prompt = f"""You are choosing which clips from the video{f' "{title}"' if title else ''} to post as short \
videos. Here are {len(cands)} candidate clips with what is said in each.
{CRITERIA}
{focus_line}{_niche_line(niche)}
On Shorts most viewers decide to stay or swipe in the first 2 seconds. Each clip shows what the viewer sees first: \
its TEASER (a 1-3 second cold open replayed before the clip) or, without one, what it OPENS WITH.

For every clip give:
- score: 0-100 for how well it would perform as a standalone short (be strict and spread the scores; only \
exceptional clips get 90+). "replay level" is real YouTube data on how much viewers rewatched that part \
(1.00 = most); treat a high level as strong evidence.
- opening: 0-100, how strongly the first 2 seconds (teaser, or opening line, plus the hook text) stop the scroll.
- tease: false if the teaser is confusing or gives the ending away (it will be dropped), otherwise true.
- title (max 8 words) and hook: improve them if you can, in the clip's language.{fit_rule}
{HOOK_RULES}

Reply as JSON: {{"clips": [{{"clip": 1, "score": 0, "opening": 0, "tease": true{fit_ask}, "title": "", "hook": "", \
"reason": ""}}]}}

{chr(10).join(blocks)}"""
    data, prov, model = ai.ask(prompt, SYSTEM, only=prov)
    rows = data.get("clips") if isinstance(data, dict) else data
    judged = 0
    for r in rows or []:
        try:
            c = cands[int(r.get("clip")) - 1]
        except (TypeError, ValueError, IndexError):
            continue
        score = _num(r.get("score"), c["score"])
        if r.get("opening") is not None:               # a weak first 2 seconds sinks a clip
            score = 0.6 * score + 0.4 * _num(r.get("opening"), score)
            c["opening"] = _num(r.get("opening"), 50)
        c["score"] = int(round(score))
        if r.get("tease") is False:
            c.pop("tease", None)
        if fit_ask and r.get("fit") is not None:
            c["fit"] = _num(r.get("fit"), 50)
        for key in ("title", "hook", "reason"):
            if str(r.get(key) or "").strip():
                c[key] = str(r[key]).strip()[:90 if key != "reason" else 240]
        judged += 1
    log.info("Judge scored %d of %d candidates (%s)", judged, len(cands), model)


def find(transcript, title, n, length_key, focus, on_progress, heatmap=None, niche=""):
    lo, hi = LENGTHS.get(length_key, (30, 60))
    words = transcript.get("words") or []
    sents = sentences(words)
    if not sents:
        sents = [{"s": s["s"], "e": s["e"], "text": s["text"]} for s in transcript.get("segments") or []
                 if s.get("text")]
    if not sents:
        raise RuntimeError("No speech was found in this video, so there's nothing to clip.")
    plist = ai.providers()
    if not plist:
        raise RuntimeError("No AI is set up. Add a Gemini or Groq key in Settings, or choose Ollama.")
    heatmap = [h for h in (heatmap or []) if "start_time" in h and "end_time" in h]
    errors = []
    for prov in plist:                                 # each AI reads in part sizes it can handle
        try:
            return _find_with(prov, sents, title, n, lo, hi, focus, heatmap, on_progress, words, niche,
                              tight=length_key == "auto")
        except ai.AIError as e:
            ai.STATUS = None
            log.warning("highlights with %s failed: %s", prov, e)
            errors.append(f"{ai.NAMES[prov]}: {e}")
            if prov != plist[-1]:
                on_progress(0.0, f"{ai.NAMES[prov]} isn't answering, trying {ai.NAMES[plist[plist.index(prov) + 1]]}...")
    raise RuntimeError("The AI couldn't pick moments. " + " | ".join(errors))


def _find_with(prov, sents, title, n, lo, hi, focus, heatmap, on_progress, words=None, niche="", tight=False):
    groups = _windows(sents, ai.WINDOW[prov])
    want = max(n * 2, n + 4)                           # scout for more, keep the best
    per = want if len(groups) == 1 else max(3, math.ceil(want * 1.3 / len(groups)))
    cands, used, failed = [], "", []
    for k, g in enumerate(groups):
        on_progress(k / (len(groups) + 1), f"AI is reading part {k + 1} of {len(groups)}..." if len(groups) > 1
                    else "AI is reading the transcript...")
        ai.STATUS = lambda m, f=k / (len(groups) + 1): on_progress(f, m)    # live "trying another model..."
        try:
            data, _p, model = ai.ask(_prompt(g, title, per, lo, hi, focus, heatmap, niche, tight), SYSTEM,
                                     only=prov)
        except ai.AIError as e:                        # one part failed: keep the other parts' moments
            failed.append(str(e))
            if len(failed) > max(1, len(groups) // 3):
                raise
            continue
        used = f"{ai.NAMES[prov]} ({model})"
        clips = data.get("clips") if isinstance(data, dict) else data
        for c in clips or []:
            if isinstance(c, dict):
                fixed = _snap(c, sents, lo, hi, words)
                if fixed:
                    cands.append(fixed)
    if not cands:
        if failed:
            raise ai.AIError(failed[-1])
        raise RuntimeError("The AI didn't suggest any usable clips. Try a different length or video.")
    # drop near-duplicates before judging (keep the higher first-pass score)
    uniq = []
    for c in sorted(cands, key=lambda c: -c["score"]):
        if all(_overlap(c, u) < 0.5 for u in uniq):
            uniq.append(c)
    uniq = uniq[:max(n + 10, min(want, {"groq": 24, "omniroute": 40}.get(prov, 60)), 8)]   # judge at most ~60 at once
    for c in uniq:
        c["replay"] = replay(c["start"], c["end"], heatmap)
    on_progress(len(groups) / (len(groups) + 1), f"AI is comparing {len(uniq)} possible clips...")
    ai.STATUS = lambda m, f=len(groups) / (len(groups) + 1): on_progress(f, m)
    try:
        _judge(uniq, sents, title, n, focus, prov, niche)
    except Exception as e:                             # the first pass is still a good answer
        log.warning("judge pass failed, using first-pass scores: %s", e)
    finally:
        ai.STATUS = None
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
    log.info("Highlights: %d candidates, %d kept (%s, replay data: %s, teasers: %d)", len(cands), len(kept), used,
             bool(heatmap), sum(1 for c in kept if c.get("tease")))
    on_progress(1.0, f"Picked the best {len(kept)} of {len(uniq)} moments with {used}"
                + (" + YouTube replay data" if heatmap else ""))
    return kept, used
