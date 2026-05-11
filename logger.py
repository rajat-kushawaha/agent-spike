"""
logger.py - Standardised logging setup used by every module.
A single call to get_logger(__name__) in each module ensures consistent
format and avoids duplicate handlers when modules import each other.
"""
from __future__ import annotations

import logging
import sys

_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_DATE_FORMAT = "%H:%M:%S"
_root_configured = False


def configure_root_logger(level: int = logging.INFO) -> None:
    """Call once from the entry-point (orchestrator) to set up the root logger."""
    global _root_configured
    if _root_configured:
        return

    from console import RichLoggingHandler

    root = logging.getLogger()
    root.setLevel(level)

    # Rich handler for WARNING+ (coloured, via rich console)
    rich_handler = RichLoggingHandler()
    rich_handler.setLevel(logging.WARNING)
    rich_handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
    root.addHandler(rich_handler)

    # Plain stdout handler for DEBUG (only active when level=DEBUG)
    if level <= logging.DEBUG:
        plain = logging.StreamHandler(sys.stdout)
        plain.setLevel(logging.DEBUG)
        plain.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATE_FORMAT))
        root.addHandler(plain)

    _root_configured = True


def get_logger(name: str, level: int | None = None) -> logging.Logger:
    """Return a named logger. configure_root_logger() need not be called first."""
    configure_root_logger()
    logger = logging.getLogger(name)
    if level is not None:
        logger.setLevel(level)
    return logger
