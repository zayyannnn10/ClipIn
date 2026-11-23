"""The AI that reads the transcript and picks the clips. Three providers, one call: ask(prompt, system).

  gemini  Google Gemini (free tier). Long context: reads a whole video at once.
  groq    Groq (free tier). Fast; free limits are small, so the transcript is read in parts.
  ollama  Local model through Ollama - no key, nothing leaves the PC. Read in parts.
"""
import json
import os
import re
import shutil
import subprocess
import time

import requests

from core.config import cfg, key
from core.log import log

GROQ = "https://api.groq.com/openai/v1"
GROQ_TEXT = ["openai/gpt-oss-120b", "llama-3.3-70b-versatile", "moonshotai/kimi-k2-instruct",
             "qwen/qwen3-32b", "openai/gpt-oss-20b", "llama-3.1-8b-instant"]
# how many seconds of transcript each provider reads per request
WINDOW = {"gemini": 4 * 3600, "claude": 3 * 3600, "groq": 480, "omniroute": 1200, "ollama": 600}
NAMES = {"gemini": "Gemini", "claude": "Claude", "groq": "Groq", "omniroute": "OmniRoute", "ollama": "Ollama (local)"}
# tokens used by the AI calls of this job (shown on the project page)
USAGE = {"input": 0, "output": 0, "cache_read": 0, "calls": 0}


def reset_usage():
    USAGE.update(input=0, output=0, cache_read=0, calls=0)


def _count(inp=0, out=0, cache=0):
    USAGE["input"] += int(inp or 0)
    USAGE["output"] += int(out or 0)
    USAGE["cache_read"] += int(cache or 0)
    USAGE["calls"] += 1


class AIError(Exception):
    pass


STATUS = None                                         # set by the caller: shows "trying another model..." on screen


def _say(msg):
    if STATUS:
        try:
            STATUS(msg)
        except Exception:
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
    order = [first] + ([p for p in ("gemini", "omniroute", "groq", "ollama") if p != first] if cfg.get("ai.fallback") else [])
    out = []
    for p in order:
        if p == "gemini" and key("gemini_api_key"):
            out.append(p)
        elif p == "groq" and key("groq_api_key"):
            out.append(p)
        elif p == "claude" and p == first and os.environ.get("GITHUB_ACTIONS") != "true":
            out.append(p)                          # Claude Code on this laptop (your Claude plan)
        elif p == "omniroute" and (p == first or key("omniroute_api_key")) and os.environ.get("GITHUB_ACTIONS") != "true":
            out.append(p)                          # a local program: never in the GitHub cloud
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
                           {"claude": _claude, "groq": _groq, "omniroute": _omniroute, "ollama": _ollama}[p](prompt, system))
            return data, p, model
        except AIError as e:
            log.warning("%s failed: %s", p, e)
            errors.append(f"{NAMES[p]}: {e}")
    raise AIError(" | ".join(errors))


# Gemini -------------------------------------------------------------------------------------------
# Free Gemini keys change often: models get retired (404), each model has its own small quota (429),
# and busy models time out (503/504). So ClipIn tries many models, waits and retries when Google says
# how long to wait, remembers which model worked last time, and never retries a retired model.
_gem_models = []
_STATE = None                                         # config/ai_state.json: {"good": model, "dead": [...]}
GEMINI_BUDGET = 8 * 60                                # give up on Gemini after this many seconds


def _state():
    global _STATE
    if _STATE is None:
        from core.paths import CONFIG
        try:
            _STATE = json.loads((CONFIG / "ai_state.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _STATE = {}
        _STATE.setdefault("dead", [])
    return _STATE


def _save_state():
    from core.paths import CONFIG
    try:
        (CONFIG / "ai_state.json").write_text(json.dumps(_STATE, indent=1), encoding="utf-8")
    except OSError:
        pass


def _ver(n):
    m = re.search(r"-(\d+(?:\.\d+)?)", n)
    return float(m.group(1)) if m else 0.0


def _gemini_models(client):
    want = cfg.get("ai.gemini_model", "auto")
    if want and want != "auto":
        return [want]
    if not _gem_models:
        names = []
        try:
            for m in client.models.list():
                if "generateContent" in (m.supported_actions or []):
                    names.append(m.name.replace("models/", ""))
        except Exception as e:
            # restricted keys may not be allowed to list models but can still generate
            log.warning("Gemini model list unavailable (%s), using known models", str(e)[:120])
        text = [n for n in names if n.startswith("gemini-") and not re.search(r"tts|image|audio|live|embed|robotics|computer", n)]
        flash = sorted((n for n in text if re.fullmatch(r"gemini-[\d.]+-flash(-preview)?", n)),
                       key=lambda n: (-_ver(n), "preview" in n))
        lite = sorted((n for n in text if re.fullmatch(r"gemini-[\d.]+-flash-lite(-preview)?", n)),
                      key=lambda n: (-_ver(n), "preview" in n))
        known = [] if flash else ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash"]
        latest = [n for n in ("gemini-flash-latest", "gemini-pro-latest", "gemini-flash-lite-latest") if n in text or not names]
        _gem_models[:] = list(dict.fromkeys(flash + known + latest + lite))
    dead = set(_state()["dead"])
    good = _state().get("good")
    models = [m for m in _gem_models if m not in dead]
    if good in models:                                # the model that answered last time goes first
        models.remove(good)
        models.insert(0, good)
    return models


def _retry_delay(s):
    """Seconds Google asks us to wait (from a 429 answer), or None."""
    m = re.search(r"retryDelay['\"]?\s*:\s*['\"]?(\d+(?:\.\d+)?)s", s) or re.search(r"retry in (\d+(?:\.\d+)?)s", s)
    return float(m.group(1)) if m else None


def _short(s):
    s = str(s)
    for code in ("404", "429", "500", "503", "504", "400"):
        if code in s[:12] or f"'code': {code}" in s:
            return {"404": "retired", "429": "quota used up", "503": "busy", "504": "too slow",
                    "500": "server error", "400": "bad request"}[code]
    return s[:80]


def _gemini(prompt, system, fast=False):
    k = key("gemini_api_key")
    if not k:
        raise AIError("no Gemini key")
    try:
        from google import genai
    except ImportError:
        raise AIError("google-genai isn't installed (pip install google-genai)") from None
    big = len(prompt) > 40_000
    client = genai.Client(api_key=k, http_options={"timeout": 240_000 if big else 120_000})   # ms
    models = _gemini_models(client)
    if fast:                                          # quick jobs: last good model, then the light ones
        good = _state().get("good")
        models = sorted(models, key=lambda m: (m != good, "lite" not in m))
    if not models:
        raise AIError("no Gemini model is available for this key")
    start, tried, waits = time.time(), {}, []
    for rnd in range(3):                              # up to 3 rounds over all models
        for m in models:
            if time.time() - start > GEMINI_BUDGET:
                break
            if tried.get(m) in ("retired", "daily quota used up"):
                continue
            try:
                t0 = time.time()
                r = client.models.generate_content(
                    model=m, contents=prompt,
                    config={"system_instruction": system, "response_mime_type": "application/json",
                            "temperature": 0.4, "automatic_function_calling": {"disable": True}})
                data = parse_json(r.text or "")
                log.info("Gemini %s answered in %.1fs", m, time.time() - t0)
                um = getattr(r, "usage_metadata", None)
                if um is not None:
                    _count(getattr(um, "prompt_token_count", 0), getattr(um, "candidates_token_count", 0),
                           getattr(um, "cached_content_token_count", 0))
                if _state().get("good") != m:
                    _STATE["good"] = m
                    _save_state()
                return data, m
            except AIError as e:                       # answered, but not valid JSON: next model
                tried[m] = "bad answer"
                log.warning("Gemini %s: %s", m, e)
            except Exception as e:
                s = str(e)
                if any(x in s for x in ("API_KEY_INVALID", "PERMISSION_DENIED", "401", "API key not valid")):
                    raise AIError("Gemini rejected the API key") from None
                why = _short(s)
                if why == "retired" or "no longer available" in s or "is not found" in s:
                    tried[m] = "retired"
                    if m not in _state()["dead"]:
                        _STATE["dead"].append(m)
                        _save_state()
                elif why == "quota used up":
                    delay = _retry_delay(s)
                    daily = "PerDay" in s or (delay is not None and delay > 120)
                    tried[m] = "daily quota used up" if daily else "quota used up"
                    if delay and not daily:
                        waits.append(delay)
                else:
                    tried[m] = why
                log.warning("Gemini %s failed: %s", m, tried[m])
                said = {"daily quota used up": "has used today's free quota", "quota used up": "is rate-limited",
                        "retired": "was retired by Google", "busy": "is busy", "too slow": "is too slow right now"}
                _say(f"Gemini {m} {said.get(tried[m], 'failed')}, trying another model "
                     f"({models.index(m) + 1} of {len(models)})...")
        usable = [m for m in models if tried.get(m) not in ("retired", "daily quota used up")]
        if not usable or time.time() - start > GEMINI_BUDGET or rnd == 2:
            break
        pause = min(60.0, max(waits) + 2) if waits else 8.0   # Google said "wait N s": wait, then try again
        log.info("All Gemini models busy or rate-limited; waiting %.0fs and trying again", pause)
        _say(f"All Gemini models are busy right now, waiting {pause:.0f}s and trying again (round {rnd + 2} of 3)...")
        time.sleep(pause)
        waits.clear()
    summary = ", ".join(f"{m} {w}" for m, w in list(tried.items())[:6])
    if tried and all(w in ("quota used up", "daily quota used up", "retired") for w in tried.values()):
        raise AIError(f"the free Gemini quota is used up for now ({summary})")
    raise AIError(f"no Gemini model answered ({summary})")


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
                _say(f"Groq asks to slow down, waiting {wait:.0f}s...")
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
        if r.status_code == 413:
            last = f"{m}: the text is too long for Groq's free limit"
            continue
        if r.status_code >= 400:
            last = f"{m}: HTTP {r.status_code} {r.text[:160]}"
            continue
        try:
            text = r.json()["choices"][0]["message"].get("content") or ""
            return parse_json(text), m
        except (AIError, KeyError, ValueError) as e:
            last = f"{m}: {e}"
    raise AIError(last)


# OmniRoute (local gateway) ------------------------------------------------------------------------
# OmniRoute runs on this PC and answers like OpenAI. Model "auto" lets it pick among every AI connected
# in its dashboard and switch to the next one when a limit is hit. Its token "compression" would rewrite
# the transcript (timestamps!), so ClipIn turns it off for every request.

def omni_url():
    return (cfg.get("ai.omni_url") or "http://localhost:20128/v1").rstrip("/")


def omni_running(timeout=3):
    try:
        requests.get(omni_url() + "/models", timeout=timeout,
                     headers={"Authorization": f"Bearer {key('omniroute_api_key')}"} if key("omniroute_api_key") else {})
        return True                                   # any answer (even 401) means the server is up
    except requests.RequestException:
        return False


def omni_start(wait=60):
    """Start OmniRoute in the background if it isn't running. Returns True when it answers."""
    if omni_running():
        return True
    exe = shutil.which("omniroute") or shutil.which("omniroute.cmd")
    if not exe:
        appdata = os.environ.get("APPDATA", "")
        cand = os.path.join(appdata, "npm", "omniroute.cmd")
        exe = cand if os.path.isfile(cand) else None
    if not exe:
        raise AIError("OmniRoute isn't installed. Install it with: npm install -g omniroute")
    _say("Starting OmniRoute in the background...")
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([exe, "serve", "--daemon", "--no-open"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, creationflags=flags, close_fds=True)
    t0 = time.time()
    while time.time() - t0 < wait:
        time.sleep(2)
        if omni_running():
            log.info("OmniRoute started in %.0fs", time.time() - t0)
            return True
    raise AIError("OmniRoute didn't start. Open a terminal and run: omniroute")


def _omniroute(prompt, system):
    url = omni_url()
    model = cfg.get("ai.omni_model") or "auto"
    if not omni_running():
        if not cfg.get("ai.omni_autostart", True):
            raise AIError("OmniRoute isn't running. Start it with: omniroute")
        omni_start()
    headers = {"x-omniroute-compression": "off", "Content-Type": "application/json"}
    k = key("omniroute_api_key")
    if k:
        headers["Authorization"] = f"Bearer {k}"
    body = {"model": model, "temperature": 0.4, "max_tokens": 8000, "stream": False,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"}}
    last = ""
    for attempt in range(3):
        try:
            r = requests.post(f"{url}/chat/completions", json=body, headers=headers, timeout=300)
        except requests.RequestException as e:
            raise AIError(f"can't reach OmniRoute ({type(e).__name__})") from None
        if r.status_code in (401, 403):
            raise AIError("OmniRoute rejected the key. Make one in its dashboard (API Keys) and paste it in Settings")
        if r.status_code == 400 and "response_format" in r.text and "response_format" in body:
            body.pop("response_format")               # some routed models don't take JSON mode
            continue
        low = r.text.lower()
        if r.status_code >= 400 and any(x in low for x in ("too long", "too large", "context length", "maximum context",
                                                           "max 6000", "413")):
            raise AIError("OmniRoute's connected AIs can't read this much text. Open its dashboard "
                          "(http://localhost:20128) and connect Gemini, Groq, NVIDIA or OpenRouter: their big models can")
        if r.status_code in (429, 502, 503, 504):     # every connected AI busy: OmniRoute says when to retry
            wait = min(60.0, float(r.headers.get("retry-after") or 10))
            last = f"HTTP {r.status_code} {r.text[:160]}"
            _say(f"OmniRoute: all connected AIs are busy, waiting {wait:.0f}s...")
            time.sleep(wait)
            continue
        if r.status_code >= 400:
            txt = r.text[:200]
            if "no eligible" in txt.lower() or "no provider" in txt.lower() or "no route" in txt.lower():
                raise AIError("OmniRoute has no AI connected. Open http://localhost:20128 and connect Gemini, "
                              "Groq or another provider")
            raise AIError(f"OmniRoute HTTP {r.status_code}: {txt}")
        try:
            j = r.json()
            text = j["choices"][0]["message"].get("content") or ""
            used = j.get("model") or model
            decision = r.headers.get("X-OmniRoute-Decision", "")
            log.info("OmniRoute answered with %s %s", used, decision[:120])
            return parse_json(text), f"{used}"
        except (KeyError, ValueError, IndexError) as e:
            last = f"bad answer: {e}"
            if isinstance(e, AIError):
                raise
    raise AIError(f"OmniRoute didn't answer ({last})")


# Claude (Claude Code on this laptop) --------------------------------------------------------------
# Runs the `claude` command in print mode with your Claude login: no API key, it uses your Claude plan.
# No tools, no MCP servers, no saved session: just one question and one JSON answer.

def claude_exe():
    """The real claude.exe (calling it directly keeps every argument exactly as sent; the .cmd launcher
    would pass them through cmd.exe)."""
    npm = os.path.join(os.environ.get("APPDATA", ""), "npm")
    direct = os.path.join(npm, "node_modules", "@anthropic-ai", "claude-code", "bin", "claude.exe")
    if os.path.isfile(direct):
        return direct
    return shutil.which("claude.exe") or shutil.which("claude") or None


def _claude(prompt, system):
    import tempfile
    exe = claude_exe()
    if not exe:
        raise AIError("Claude Code isn't installed. Install it from claude.com/code, then sign in once.")
    model = cfg.get("ai.claude_model") or "sonnet"
    args = [exe, "-p", "--output-format", "json", "--model", model, "--tools", "", "--no-session-persistence",
            "--strict-mcp-config", "--system-prompt", system]
    effort = cfg.get("ai.claude_effort") or ""          # low | medium | high: less = faster, lighter on the plan
    if effort:
        args += ["--effort", effort]
    # only your own Claude login: drop settings inherited from another Claude program (session proxies, keys)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("CLAUDE", "ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"))}
    t0 = time.time()
    _say(f"Asking Claude ({model})...")
    try:
        r = subprocess.run(args, input=prompt, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=900, cwd=tempfile.gettempdir(), env=env,
                           creationflags=0x08000000 if os.name == "nt" else 0)
    except subprocess.TimeoutExpired:
        raise AIError("Claude took too long (15 min)") from None
    try:
        j = json.loads(r.stdout)
    except ValueError:
        raise AIError(f"Claude Code didn't answer ({(r.stderr or r.stdout)[-200:].strip()})") from None
    u = j.get("usage") or {}
    _count(u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0), u.get("output_tokens", 0),
           u.get("cache_read_input_tokens", 0))
    text = str(j.get("result") or "")
    if j.get("is_error"):
        low = text.lower()
        if "authenticat" in low or "oauth" in low or "login" in low or "log in" in low:
            raise AIError("Claude Code isn't signed in. Open a terminal, run: claude   then type /login")
        if "limit" in low or "rate" in low or "quota" in low:
            raise AIError(f"your Claude plan's limit is reached for now ({text[:160]})")
        raise AIError(f"Claude: {text[:200]}")
    log.info("Claude %s answered in %.1fs (in %s, out %s, cached %s tokens)", model, time.time() - t0,
             u.get("input_tokens"), u.get("output_tokens"), u.get("cache_read_input_tokens"))
    return parse_json(text), model


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
