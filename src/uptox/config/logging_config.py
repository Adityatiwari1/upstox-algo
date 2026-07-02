import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def setup() -> None:
    Path("logs").mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")

    fh = RotatingFileHandler(
        "logs/uptox.log", maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    fh.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(fh)
    # No StreamHandler — Textual renders stderr/stdout directly in the TUI,
    # making log lines bleed into the UI. File-only logging keeps the UI clean.
