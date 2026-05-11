"""
logger.py - Standardised logging setup used by every module.
All output (including DEBUG) goes through the Rich console so the terminal
stays readable during a demo. Plain stdout is never written to directly.
"""
from __future__ import annotations

import logging
import sys

_FORMAT_RICH = "%(name)s: %(message)s"
_FORMAT_PLAIN = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_DATE_FORMAT = "%H:%M:%S"
_root_configured = False


def configure_root_logger(level: int = logging.INFO) -> None:
    """Call once from the entry-point to set up the root logger."""
    global _root_configured
    if _root_configured:
        return

    from console import RichLoggingHandler

    root = logging.getLogger()
    root.setLevel(level)

    rich_handler = RichLoggingHandler()
    rich_handler.setLevel(level)
    rich_handler.setFormatter(logging.Formatter(_FORMAT_RICH))
    root.addHandler(rich_handler)

    _root_configured = True


def get_logger(name: str, level: int | None = None) -> logging.Logger:
    """Return a named logger. configure_root_logger() need not be called first."""
    configure_root_logger()
    logger = logging.getLogger(name)
    if level is not None:
        logger.setLevel(level)
    return logger
