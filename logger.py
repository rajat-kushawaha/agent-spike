"""
logger.py - Standardised logging setup used by every module.
A single call to get_logger(__name__) in each module ensures consistent
format and avoids duplicate handlers when modules import each other.
"""
from __future__ import annotations

import logging
import sys
from typing import Optional


_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"
_root_configured = False


def configure_root_logger(level: int = logging.INFO) -> None:
    """Call once from the entry-point (orchestrator) to set up the root logger."""
    global _root_configured
    if _root_configured:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATE_FORMAT))
    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(handler)
    _root_configured = True


def get_logger(name: str, level: Optional[int] = None) -> logging.Logger:
    """Return a named logger. configure_root_logger() need not be called first."""
    configure_root_logger()
    logger = logging.getLogger(name)
    if level is not None:
        logger.setLevel(level)
    return logger
