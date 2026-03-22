"""
Structured rotating-file logger shared across all modules.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys

import config as cfg


def setup_logger(name: str = "polybot") -> logging.Logger:
    os.makedirs(cfg.LOG_DIR, exist_ok=True)

    logger = logging.getLogger(name)
    if logger.handlers:
        return logger   # already configured

    level = getattr(logging, cfg.LOG_LEVEL.upper(), logging.INFO)
    logger.setLevel(level)

    fmt = logging.Formatter(
        "%(asctime)s.%(msecs)03d | %(levelname)-8s | %(name)-20s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Rotating file handler (50 MB per file, keep 10 backups)
    fh = logging.handlers.RotatingFileHandler(
        os.path.join(cfg.LOG_DIR, f"{name}.log"),
        maxBytes=cfg.LOG_ROTATE_MB * 1024 * 1024,
        backupCount=10,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    # Stream handler for terminal output
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    sh.setLevel(logging.WARNING)   # keep terminal quiet; dashboard handles display
    logger.addHandler(sh)

    return logger
