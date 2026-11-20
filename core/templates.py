"""Video templates: one click sets format, framing, caption style and effects together."""

# format and layout are NOT in BASE: a caption template keeps the framing the user picked
BASE = {"caption_style": "Bold Pop", "hook_on": True, "zoom": False,
        "bar": False, "bar_color": "FFFFFF"}

TEMPLATES = [
    {"id": "viral", "name": "Viral Pop", "desc": "Yellow word pop, zoom cuts, progress bar",
     "caption_style": "Bold Pop", "zoom": True, "bar": True, "bar_color": "FFD400"},
    {"id": "hype", "name": "Hype", "desc": "One huge word at a time, fast zooms",
     "caption_style": "One Word", "zoom": True, "bar": True, "bar_color": "FF7A1A"},
    {"id": "hustle", "name": "Hustle", "desc": "Two-tone punchy words, zoom cuts",
     "caption_style": "Two Tone", "zoom": True},
    {"id": "podcast", "name": "Podcast", "desc": "Calm clean captions, no effects",
     "caption_style": "Clean"},
    {"id": "neon", "name": "Neon Night", "desc": "Glowing green words, green bar",
     "caption_style": "Neon", "zoom": True, "bar": True, "bar_color": "39FF88"},
    {"id": "fun", "name": "Fun", "desc": "Colourful playful words",
     "caption_style": "Rainbow", "zoom": True},
    {"id": "aesthetic", "name": "Aesthetic", "desc": "Soft pink lowercase, no zoom",
     "caption_style": "Pastel", "bar": True, "bar_color": "F9A8D4"},
    {"id": "duo", "name": "Podcast Duo", "desc": "Split screen: one person per half",
     "layout": "split_two", "caption_style": "Bold Pop", "zoom": False, "bar": True, "bar_color": "FFFFFF"},
    {"id": "reaction", "name": "Face + Scene", "desc": "Speaker on top, full shot below",
     "layout": "split_wide", "caption_style": "Two Tone", "zoom": False},
    {"id": "cinematic", "name": "Cinematic", "desc": "Full shot on blur, small subtitles",
     "layout": "fit", "caption_style": "Subtitle", "hook_on": False},
    {"id": "square", "name": "Square Feed", "desc": "1:1 on black, boxed captions",
     "format": "1:1", "layout": "fit_black", "caption_style": "Boxed"},
    {"id": "wide", "name": "YouTube Wide", "desc": "16:9 for normal YouTube videos",
     "format": "16:9", "layout": "fit_black", "caption_style": "Clean", "hook_on": False},
]


def get(tid):
    t = next((t for t in TEMPLATES if t["id"] == tid), None)
    return {**BASE, **t} if t else None
