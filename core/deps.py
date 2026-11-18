"""ClipIn's Python add-ons: check them and install any that are missing (for this Python, this user).
Runs in the background worker when ClipIn starts, and again right before a download if needed."""
import importlib
import importlib.util
import subprocess
import sys

from core.log import log

# import name -> pip package
NEEDED = {
    "yt_dlp": "yt-dlp[default]",
    "requests": "requests",
    "numpy": "numpy",
    "cv2": "opencv-python",
    "google.genai": "google-genai",
    "faster_whisper": "faster-whisper",
    "imageio_ffmpeg": "imageio-ffmpeg",
    "nacl": "pynacl",
    "websockets": "websockets",
    "qrcode": "qrcode",
    "psutil": "psutil",
}
NO_WINDOW = 0x08000000


def missing():
    out = []
    for mod, pkg in NEEDED.items():
        try:
            if importlib.util.find_spec(mod) is None:
                out.append(pkg)
        except (ImportError, ValueError):
            out.append(pkg)
    return out


def install(pkgs, say=print):
    if not pkgs:
        return True
    say(f"Installing missing parts: {', '.join(pkgs)}...")
    r = subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-q", *pkgs],
                       capture_output=True, text=True, creationflags=NO_WINDOW)
    if r.returncode != 0:
        log.error("pip install %s failed: %s", pkgs, r.stderr[-800:])
        raise RuntimeError(f"Couldn't install {', '.join(pkgs)}: {r.stderr.strip()[-300:]}")
    importlib.invalidate_caches()
    log.info("Installed %s", pkgs)
    return True


def ensure(say=print):
    """Install whatever is missing. Returns the list that was installed."""
    need = missing()
    if need:
        install(need, say)
    return need


def require(module, say=print):
    """Import a module, installing its package first if it's missing."""
    try:
        return importlib.import_module(module)
    except ImportError:
        install([NEEDED.get(module, module)], say)
        return importlib.import_module(module)
