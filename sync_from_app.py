"""Copy the shared code and assets from the laptop app (../ClipIn AI) into this GitHub folder.
Run it after changing the laptop app, then push. The cloud worker always uses the same clip engine."""
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP = HERE.parent / "ClipIn AI"
SKIP = {"youtube_upload.py", "worker.py", "previews.py", "github_setup.py"}      # laptop-only


def main():
    dst = HERE / "core"
    shutil.rmtree(dst, ignore_errors=True)
    dst.mkdir()
    for f in sorted((APP / "core").glob("*.py")):
        if f.name not in SKIP:
            shutil.copy2(f, dst / f.name)
    caps = HERE / "docs" / "captions"
    caps.mkdir(parents=True, exist_ok=True)
    for f in (APP / "app" / "assets" / "captions").glob("*.png"):
        shutil.copy2(f, caps / f.name)
    print(f"Copied {len(list(dst.glob('*.py')))} core files and {len(list(caps.glob('*.png')))} caption previews.")


if __name__ == "__main__":
    main()
