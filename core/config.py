"""Settings (config/settings.json) and API keys (config/secrets.json, git-ignored).

Keys: ClipIn's own key wins. When it is empty and "use FRIDAY's keys" is on, the key is read from
FRIDAY's secrets file each time it is needed (never copied into ClipIn's files).
"""
import copy
import json
import os
import threading
from pathlib import Path

from core.paths import CONFIG, ROOT

DEFAULTS = {
    "ai": {
        "provider": "gemini",          # gemini | groq | ollama  (picks the clip moments)
        "fallback": True,              # if the chosen AI fails, try the others that have a key
        "gemini_model": "auto",
        "groq_model": "auto",
        "ollama_model": "qwen2.5:7b",
        "ollama_url": "http://localhost:11434",
        "ollama_ctx": 8192,
    },
    "transcribe": {
        "engine": "auto",              # auto | local_gpu | local_cpu | groq
        "model": "small",              # local Whisper model
    },
    "download": {
        # used only when YouTube says "Sign in to confirm you're not a bot"
        "cookies_browser": "none",     # none | chrome | edge | firefox | brave | opera
        "cookies_file": "",            # a cookies.txt exported from the browser (works while it's open)
        "connected": False,            # last "Connect YouTube" check succeeded
    },
    "phone": {
        "repo": "",                    # username/clipin - the GitHub repository that runs the phone cloud
    },
    "storage": {
        "keep_source": False,          # False = delete a downloaded video once its clips are made
    },
    "render": {
        "encoder": "auto",             # auto | nvenc | x264
        "export_dir": str(Path.home() / "Videos"),   # only where the Download dialog starts
    },
    "new": {                           # last choices on the New clips page
        "length": "30-60",
        "count": 6,
        "format": "9:16",
        "layout": "auto",
        "caption_style": "Bold Pop",
        "language": "auto",
        "hook_on": True,
        "focus": "",
    },
    "keys": {
        "use_friday": True,
        "friday_secrets": str(ROOT.parent / "FRIDAY" / "config" / "secrets.json"),
    },
}

SECRET_KEYS = {"gemini_api_key": "", "groq_api_key": "", "github_token": ""}


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


class Store:
    def __init__(self, path, defaults):
        self.path, self.defaults = path, defaults
        self.lock = threading.RLock()
        self.data = copy.deepcopy(defaults)
        self.load()

    def load(self):
        try:
            text = self.path.read_text(encoding="utf-8-sig") if self.path.exists() else ""
            self.data = _merge(self.defaults, json.loads(text) if text.strip() else {})
        except (OSError, ValueError):
            self.data = copy.deepcopy(self.defaults)

    def save(self):
        with self.lock:
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)

    def get(self, dotted, default=None):
        cur = self.data
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def set(self, dotted, value, save=True):
        with self.lock:
            cur = self.data
            parts = dotted.split(".")
            for p in parts[:-1]:
                cur = cur.setdefault(p, {})
            cur[parts[-1]] = value
            if save:
                self.save()


cfg = Store(CONFIG / "settings.json", DEFAULTS)
secrets = Store(CONFIG / "secrets.json", SECRET_KEYS)


def friday_key(name):
    try:
        p = Path(cfg.get("keys.friday_secrets") or "")
        if not p.is_file():
            return ""
        return str(json.loads(p.read_text(encoding="utf-8-sig")).get(name) or "").strip()
    except (OSError, ValueError):
        return ""


def key(name):
    env = os.environ.get(name.upper(), "").strip()    # GitHub cloud worker: keys come from repo secrets
    if env:
        return env
    own = str(secrets.get(name) or "").strip()
    if own:
        return own
    return friday_key(name) if cfg.get("keys.use_friday") else ""


def key_source(name):
    """'own', 'friday' or '' - where the key in use comes from."""
    if str(secrets.get(name) or "").strip():
        return "own"
    if cfg.get("keys.use_friday") and friday_key(name):
        return "friday"
    return ""
