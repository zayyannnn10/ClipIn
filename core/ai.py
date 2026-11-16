"""The AI that reads the transcript and picks the clips. Three providers, one call: ask(prompt, system).

  gemini  Google Gemini (free tier). Long context: reads a whole video at once.
  groq    Groq (free tier). Fast; free limits are small, so the transcript is read in parts.
  ollama  Local model through Ollama - no key, nothing leaves the PC. Read in parts.
"""
import json
import re
import time

import requests

from core.config import cfg, key
from core.log import log

GROQ = "https://api.groq.com/openai/v1"
GROQ_TEXT = ["openai/gpt-oss-120b", "llama-3.3-70b-versatile", "moonshotai/kimi-k2-instruct",
             "qwen/qwen3-32b", "openai/gpt-oss-20b", "llama-3.1-8b-instant"]
# how many seconds of transcript each provider reads per request
WINDOW = {"gemini": 4 * 3600, "groq": 480, "ollama": 600}
FAST_GEMINI = ("gemini-3.5-flash", "gemini-2.5-flash")
NAMES = {"gemini": "Gemini", "groq": "Groq", "ollama": "Ollama (local)"}


class AIError(Exception):
    pass


def parse_json(text):
    t = (text or "").strip()
    t = re.sub(r"(?s)<think>.*?</think>", "", t).strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    try:
        return json.loads(t)
    except ValueError:
        pass
    m = re.search(r"\{.*\}", t, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except ValueError:
            pass
    raise AIError("The AI's answer wasn't valid JSON")


def providers():
    """Providers to try, chosen one first."""
    first = cfg.get("ai.provider", "gemini")
    order = [first] + ([p for p in ("gemini", "groq", "ollama") if p != first] if cfg.get("ai.fallback") else [])
    out = []
    for p in order:
        if p == "gemini" and key("gemini_api_key"):
            out.append(p)
        elif p == "groq" and key("groq_api_key"):
            out.append(p)
        elif p == "ollama" and p == first:         # local AI only when chosen (it may not be installed)
            out.append(p)
    return out


def ask(prompt, system, only=None, fast=False):
    """Returns (parsed_json, provider, model). Tries the providers in order.
    fast=True: quick jobs (titles, hashtags) prefer the proven-fast Gemini model over the newest one."""
    plist = [only] if only else providers()
    if not plist:
        raise AIError("No AI is set up. Add a Gemini or Groq key in Settings, or choose Ollama.")
    errors = []
    for p in plist:
        try:
            data, model = (_gemini(prompt, system, fast) if p == "gemini" else
                           {"groq": _groq, "ollama": _ollama}[p](prompt, system))
            return data, p, model
        except AIError as e:
            log.warning("%s failed: %s", p, e)
            errors.append(f"{NAMES[p]}: {e}")
    raise AIError(" | ".join(errors))


# Gemini -------------------------------------------------------------------------------------------
_gem_models = []


def _gemini_models(client):
    want = cfg.get("ai.gemini_model", "auto")
    if want and want != "auto":
        return [want]
    if _gem_models:
        return _gem_models
    names = []
    try:
        for m in client.models.list():
            if "generateContent" in (m.supported_actions or []):
                names.append(m.name.replace("models/", ""))
    except Exception as e:
        # newer restricted keys may not be allowed to list models but can still generate; the
        # generate call below decides whether the key really works
        log.warning("Gemini model list unavailable (%s), using known models", str(e)[:120])

    def ver(n):
        m = re.search(r"-(\d+(?:\.\d+)?)", n)
        return float(m.group(1)) if m else 0.0
    flash = sorted((n for n in names if re.fullmatch(r"gemini-[\d.]+-flash(-preview)?", n)),
                   key=lambda n: (-ver(n), "preview" in n))
    known = [] if flash else ["gemini-3.6-flash", "gemini-3.5-flash"]
    _gem_models[:] = list(dict.fromkeys(flash[:4] + known + ["gemini-flash-latest", "gemini-2.5-flash"]))
    return _gem_models


def _gemini(prompt, system, fast=False):
    k = key("gemini_api_key")
    if not k:
        raise AIError("no Gemini key")
    try:
        from google import genai
    except ImportError:
        raise AIError("google-genai isn't installed (pip install google-genai)") from None
    client = genai.Client(api_key=k, http_options={"timeout": 120_000})   # ms; a stuck model moves on
    last = None
    models = _gemini_models(client)
    if fast:
        models = sorted(models, key=lambda m: 0 if m in FAST_GEMINI else 1)
    for m in models:
        try:
            t0 = time.time()
            r = client.models.generate_content(
                model=m, contents=prompt,
                config={"system_instruction": system, "response_mime_type": "application/json",
                        "temperature": 0.4, "automatic_function_calling": {"disable": True}})
            log.info("Gemini %s answered in %.1fs", m, time.time() - t0)
            return parse_json(r.text or ""), m
        except AIError as e:
            last = e
        except Exception as e:
            s = str(e)
            if any(x in s for x in ("API_KEY_INVALID", "PERMISSION_DENIED", "401")):
                raise AIError("Gemini rejected the API key") from None
            last = e                                  # 404 / quota / busy: try the next model
    raise AIError(f"no Gemini model answered ({str(last)[:200]})")


# Groq ---------------------------------------------------------------------------------------------
_groq_ids = []


def _groq_models(k):
    want = cfg.get("ai.groq_model", "auto")
    if want and want != "auto":
        return [want]
    if not _groq_ids:
        try:
            r = requests.get(f"{GROQ}/models", headers={"Authorization": f"Bearer {k}"}, timeout=15)
            if r.status_code in (401, 403):
                raise AIError("Groq rejected the API key")
            _groq_ids[:] = [m["id"] for m in r.json().get("data", []) if m.get("active", True)]
        except requests.RequestException as e:
            raise AIError(f"can't reach Groq ({e})") from None
    return [m for m in GROQ_TEXT if m in _groq_ids] or GROQ_TEXT[:2]


def _groq(prompt, system):
    k = key("groq_api_key")
    if not k:
        raise AIError("no Groq key")
    last = "no model answered"
    for m in _groq_models(k)[:3]:
        body = {"model": m, "temperature": 0.4, "max_completion_tokens": 4000,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                "response_format": {"type": "json_object"}}
        if m.startswith("openai/gpt-oss"):
            body["reasoning_effort"] = "low"
        r = None
        for _attempt in range(4):
            try:
                r = requests.post(f"{GROQ}/chat/completions", json=body, timeout=120,
                                  headers={"Authorization": f"Bearer {k}"})
            except requests.RequestException as e:
                last = str(e)
                r = None
                break
            if r.status_code == 429:
                wait = float(r.headers.get("retry-after") or 15)
                if wait > 90:
                    break
                time.sleep(wait + 1)
                continue
            if r.status_code == 400 and "response_format" in r.text and "response_format" in body:
                body.pop("response_format")
                continue
            break
        if r is None:
            continue
        if r.status_code in (401, 403):
            raise AIError("Groq rejected the API key")
        if r.status_code >= 400:
            last = f"{m}: HTTP {r.status_code} {r.text[:160]}"
            continue
        try:
            text = r.json()["choices"][0]["message"].get("content") or ""
            return parse_json(text), m
        except (AIError, KeyError, ValueError) as e:
            last = f"{m}: {e}"
    raise AIError(last)


# Ollama (local) -----------------------------------------------------------------------------------

def _ollama(prompt, system):
    url = (cfg.get("ai.ollama_url") or "http://localhost:11434").rstrip("/")
    model = cfg.get("ai.ollama_model") or "qwen2.5:7b"
    body = {"model": model, "stream": False, "format": "json", "keep_alive": 0,   # free the GPU right after
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            "options": {"num_ctx": int(cfg.get("ai.ollama_ctx") or 8192), "temperature": 0.4}}
    try:
        r = requests.post(f"{url}/api/chat", json=body, timeout=900)
    except requests.ConnectionError:
        raise AIError("Ollama isn't running. Install it from ollama.com, then run: ollama pull " + model) from None
    except requests.RequestException as e:
        raise AIError(str(e)) from None
    if r.status_code == 404:
        raise AIError(f"Ollama doesn't have '{model}'. Run: ollama pull {model}")
    if r.status_code >= 400:
        raise AIError(f"Ollama HTTP {r.status_code}: {r.text[:160]}")
    return parse_json(r.json().get("message", {}).get("content", "")), model
