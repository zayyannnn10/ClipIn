"""Animated captions as an .ass subtitle file (FFmpeg burns it into the clip).

Each style sets the font, colours and how words appear:
  highlight  the line stays, the spoken word lights up (colours can cycle word by word)
  reveal     words appear one by one as they're spoken
  single     one big word at a time
  plain      the whole line at once
"""
import re
from pathlib import Path

S = lambda **k: k  # noqa: E731  (keeps the table below compact)

# Fonts ship with ClipIn (fonts/, free OFL licences) so captions look the same on the laptop and in the cloud.
# bold=False for single-weight display fonts, so they aren't artificially thickened.
STYLES = {
    "Bold Pop":    S(font="Montserrat Black", hi="FFD400", ow=.07, sh=.05, case="upper", words=3, mode="highlight",
                     pop=True, desc="Yellow word pop"),
    "Two Tone":    S(font="Montserrat Black", hi=["FFD400", "4ADE80"], ow=.07, sh=.05, case="upper", words=2,
                     mode="highlight", pop=True, anim="pop", desc="Yellow / green, punchy"),
    "One Word":    S(font="Anton", bold=False, size=1.6, ow=.06, sh=.05, case="upper", words=1, mode="single",
                     anim="pop", pos="mid", desc="One huge word at a time"),
    "Neon":        S(font="Poppins ExtraBold", hi="39FF88", outline="0F7A43", ow=.05, case="upper", words=2,
                     mode="highlight", pop=True, glow="39FF88", desc="Glowing green"),
    "Fire":        S(font="Anton", bold=False, size=1.3, hi="FF8A1F", outline="1A0600", ow=.06, sh=.05, case="upper",
                     words=3, mode="highlight", pop=True, anim="pop", desc="Hot orange, bold"),
    "Ice":         S(font="Bebas Neue", bold=False, size=1.45, hi="7FE3FF", outline="06192E", ow=.05, sh=.05,
                     case="upper", words=3, mode="highlight", pop=True, desc="Cool cyan, tall letters"),
    "Rainbow":     S(font="Bangers", bold=False, size=1.25, hi=["FF5A5A", "FFD400", "4ADE80", "38BDF8", "C084FC"],
                     ow=.07, sh=.05, case="upper", words=3, mode="highlight", pop=True, anim="pop",
                     desc="Playful comic colours"),
    "Pastel":      S(font="Poppins ExtraBold", hi="F9A8D4", outline="3B1F35", ow=.06, sh=.04, case="lower", words=3,
                     mode="highlight", anim="pop", desc="Soft pink, lowercase"),
    "Karaoke":     S(font="Montserrat ExtraBold", hi="FF4FA3", ow=.06, sh=.05, case="upper", words=4,
                     mode="highlight", desc="Pink sing-along"),
    "Boxed":       S(font="Poppins ExtraBold", hi="FFD400", box=True, ow=.24, words=3, mode="highlight",
                     desc="Dark box behind text"),
    "Typewriter":  S(font="Poppins", size=.9, box=True, ow=.26, words=5, mode="reveal", desc="Words type in"),
    "Clean":       S(font="Poppins", size=.85, ow=.05, sh=.05, words=5, mode="plain", anim="fade",
                     desc="Simple and tidy"),
    "Subtitle":    S(font="Poppins", size=.66, box=True, ow=.3, words=8, mode="plain", pos="bottom",
                     desc="Small, like TV subtitles"),
}
NAMES = list(STYLES) + ["None"]
DEFAULTS = dict(size=1.0, primary="FFFFFF", hi="FFFFFF", outline="000000", ow=.06, sh=0.0, case=None, words=3,
                mode="highlight", pop=False, anim=None, pos="low", box=False, blur=0, glow="", bold=True)
FONTS = Path(__file__).resolve().parent.parent / "fonts"


def style(name):
    st = STYLES.get(name)
    return {**DEFAULTS, **st} if st else None


def _c(hexrgb, alpha=0):
    h = hexrgb.strip("#")
    return f"&H{alpha:02X}{h[4:6]}{h[2:4]}{h[0:2]}".upper()


def _t(sec):
    cs = int(round(max(0.0, sec) * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _esc(text):
    return text.replace("\\", "/").replace("{", "(").replace("}", ")")


def _case(word, case):
    return word.upper() if case == "upper" else word.lower() if case == "lower" else word


def _groups(words, k):
    out, cur = [], []
    for w in words:
        if cur and (len(cur) >= k or w["s"] - cur[-1]["e"] > 0.5
                    or (k >= 3 and re.search(r"[.,!?;:]$", cur[-1]["w"]))):
            out.append(cur)
            cur = []
        cur.append(w)
    if cur:
        out.append(cur)
    return out


ANIM = {"pop": "\\fscx78\\fscy78\\t(0,110,\\fscx100\\fscy100)", "fade": "\\fad(90,0)", None: ""}


def _events(st, words, dur):
    """Dialogue lines for the captions (times relative to the clip)."""
    groups = _groups(words, st["words"])
    lead = f"\\blur{st['blur'] or 0.7}"            # soft edges look far cleaner than hard pixels
    cycle = st["hi"] if isinstance(st["hi"], list) else [st["hi"]]
    out, n = [], 0
    glow = (f"{{\\1a&HFF&\\3c{_c(st['glow'])}\\3a&H40&\\bord14\\blur12\\shad0}}" if st["glow"] else "")

    def line(a, b, text):
        if b - a >= 0.02:
            if glow:                                   # coloured haze under the text
                out.append(f"Dialogue: 0,{_t(a)},{_t(b)},Cap,,0,0,0,,{glow}{text.replace(lead, '', 1)}")
            out.append(f"Dialogue: 1,{_t(a)},{_t(b)},Cap,,0,0,0,,{text}")

    for gi, g in enumerate(groups):
        g_start = max(0.0, g[0]["s"])
        nxt = groups[gi + 1][0]["s"] if gi + 1 < len(groups) else dur
        g_end = min(g[-1]["e"] + 0.25, nxt, dur)
        if g_end <= g_start:
            n += len(g)
            continue
        if st["mode"] == "plain":
            line(g_start, g_end, "{" + lead + ANIM[st["anim"]] + "}" + _esc(" ".join(w["w"] for w in g)))
            n += len(g)
            continue
        for wi, w in enumerate(g):
            a = g_start if wi == 0 else max(g_start, w["s"])
            b = min(max(g[wi + 1]["s"] if wi + 1 < len(g) else g_end, a + 0.04), g_end)
            col = _c(cycle[(n + wi) % len(cycle)])
            if st["mode"] == "single":
                end = min(max(w["e"] + 0.12, a + 0.2), nxt if wi + 1 == len(g) else b, dur)
                line(a, max(end, b), "{" + lead + ANIM[st["anim"]] + "}" + _esc(w["w"]))
                continue
            anim = ANIM[st["anim"]] if wi == 0 else ""
            parts = []
            for xi, x in enumerate(g):
                if st["mode"] == "reveal" and xi > wi:
                    break                              # words not spoken yet aren't shown
                elif xi == wi and st["mode"] == "highlight":
                    pop = "\\fscx112\\fscy112" if st["pop"] else ""
                    undo = "\\fscx100\\fscy100" if st["pop"] else ""
                    parts.append(f"{{\\c{col}{pop}}}{_esc(x['w'])}{{\\c{_c(st['primary'])}{undo}}}")
                else:
                    parts.append(_esc(x["w"]))
            line(a, b, "{" + lead + anim + "}" + " ".join(parts))
        n += len(g)
    return out


def build(words, clip_start, clip_end, width, height, style_name, hook_text, hook_on, path, pos=None):
    """Write the .ass file; returns True if it has anything to show. pos overrides the style's position
    ("mid" puts captions over the seam of split-screen layouts)."""
    st = style(style_name)
    if st and pos:
        st["pos"] = pos
    base = min(width, height) * 0.085
    vertical = height > width
    ml = round(width * 0.07)
    styles, events = [], []
    dur = clip_end - clip_start

    if st:
        size = round(base * st["size"])
        align, mv = {"mid": (5, 0), "bottom": (2, round(height * (0.07 if vertical else 0.06))),
                     "low": (2, round(height * (0.2 if vertical else 0.1)))}[st["pos"]]
        bs = 3 if st["box"] else 1
        back = _c("000000", 0x40) if st["box"] else _c("000000", 0x70)   # soft, see-through drop shadow
        outc = _c("000000", 0x40) if st["box"] else _c(st["outline"])
        bold = -1 if st["bold"] else 0
        styles.append(f"Style: Cap,{st['font']},{size},{_c(st['primary'])},{_c(st['primary'])},{outc},{back},"
                      f"{bold},0,0,0,100,100,0,0,{bs},{max(1, round(size * st['ow']))},{round(size * st['sh'])},"
                      f"{align},{ml},{ml},{mv},1")
        ws = [{"w": _case(w["w"], st["case"]), "s": w["s"] - clip_start, "e": w["e"] - clip_start}
              for w in words if w["e"] > clip_start and w["s"] < clip_end]
        events += _events(st, ws, dur)

    if hook_on and hook_text and hook_text.strip():
        # the hook title: clean white text with a soft dark outline and shadow, near the top
        hs = round(base * 0.95)
        styles.append(f"Style: Hook,Montserrat ExtraBold,{hs},{_c('FFFFFF')},{_c('FFFFFF')},{_c('000000', 0x20)},"
                      f"{_c('000000', 0x60)},-1,0,0,0,100,100,0,0,1,{max(2, round(hs * 0.08))},{round(hs * 0.06)},"
                      f"8,{ml},{ml},{round(height * 0.1)},1")
        events.append(f"Dialogue: 2,{_t(0)},{_t(min(3.2, max(1.0, dur)))},Hook,,0,0,0,,"
                      f"{{\\fad(180,260)\\blur0.8\\fscx92\\fscy92\\t(0,180,\\fscx100\\fscy100)}}"
                      f"{_esc(hook_text.strip())}")

    if not events:
        return False
    text = "\n".join([
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {width}", f"PlayResY: {height}", "WrapStyle: 0",
        "ScaledBorderAndShadow: yes", "YCbCr Matrix: TV.709", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
        "MarginR, MarginV, Encoding",
        *styles, "",
        "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        *events, ""])
    path.write_text(text, encoding="utf-8-sig")
    return True
