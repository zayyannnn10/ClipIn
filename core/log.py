"""Logging to logs/<name>.log (the window and the background worker each get their own file)."""
import logging
from logging.handlers import RotatingFileHandler

from core.paths import LOGS

log = logging.getLogger("clipin")


def setup(name="clipin", console=False):
    if log.handlers:
        return log
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = RotatingFileHandler(LOGS / f"{name}.log", maxBytes=2_000_000, backupCount=2, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if console:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        log.addHandler(sh)
    return log
