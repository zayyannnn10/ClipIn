"""Where ClipIn keeps things. Everything lives next to the app, so the folder can be moved as a whole."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config"
PROJECTS = ROOT / "projects"
LOGS = ROOT / "logs"

for _d in (CONFIG, PROJECTS, LOGS):
    _d.mkdir(exist_ok=True)
