"""AI-written YouTube Shorts details for a clip: title, description, hashtags and search tags."""
import re

from core import ai
from core.highlights import sentences

SYSTEM = ("You are a YouTube Shorts growth expert. You write titles, descriptions and hashtags that get "
          "clicks and reach without being misleading. You answer with JSON only.")


def _clip_text(clip, transcript):
    s, e = float(clip["start"]), float(clip["end"])
    return " ".join(x["text"] for x in sentences(transcript.get("words") or []) if x["e"] > s and x["s"] < e)


def clean_tag(t):
    t = re.sub(r"[^\w]", "", str(t).lstrip("#"), flags=re.UNICODE)
    return f"#{t.lower()}" if t else ""


def generate(clip, transcript, video_title="", channel_hint=""):
    text = _clip_text(clip, transcript)[:4000]
    niche = (channel_hint or "").strip()
    niche_line = (f"\nThe channel's niche: {niche}. Write for that audience and keep the broad hashtags and tags "
                  f"the same niche every time, so YouTube learns who to show the channel to.\n" if niche else "")
    prompt = f"""Write YouTube Shorts upload details for this clip.
{niche_line}
Original long video: "{video_title}"
Clip title idea: "{clip.get('title', '')}"
Clip hook: "{clip.get('hook', '')}"
What is said in the clip:
\"\"\"{text}\"\"\"

Rules:
- title: catchy, max 70 characters, no hashtags, no clickbait lies, same language as the clip.
- description: 2-3 short lines that tease the moment and invite a comment or follow. No hashtags in it.
- hashtags: 8-12 hashtags for YouTube Shorts. First "#shorts", then 2-3 broad ones for the niche, then
  specific ones about the people, topic or event in this clip. Lowercase, no spaces, start with #.
- tags: 10-15 plain search keywords/phrases (no #) people would type to find this.

Reply as JSON: {{"title": "", "description": "", "hashtags": ["#shorts"], "tags": [""]}}"""
    data, prov, model = ai.ask(prompt, SYSTEM, fast=True)
    tags_h = [clean_tag(h) for h in (data.get("hashtags") or [])]
    tags_h = [h for i, h in enumerate(tags_h) if h and h not in tags_h[:i]]
    if "#shorts" not in tags_h:
        tags_h.insert(0, "#shorts")
    return {"title": str(data.get("title") or clip.get("title", ""))[:100].strip(),
            "description": str(data.get("description") or "").strip(),
            "hashtags": tags_h[:15],
            "tags": [str(t).strip()[:60] for t in (data.get("tags") or []) if str(t).strip()][:20],
            "ai": f"{ai.NAMES.get(prov, prov)} ({model})"}


def full_description(desc, hashtags):
    """YouTube shows the first three hashtags of the description above the title."""
    return (desc.strip() + "\n\n" + " ".join(hashtags)).strip()
