"""
knowledge_loader.py - Loads agent knowledge files from the knowledge/ directory.

Files are re-read on every call so live edits take effect without restarting
the orchestrator. Each agent has three files: skills.md, rules.md, knowledge.md.
"""
from __future__ import annotations

from pathlib import Path

from logger import get_logger

log = get_logger(__name__)

_KNOWLEDGE_DIR = Path(__file__).parent / "knowledge"
_FILES = ("skills.md", "rules.md", "knowledge.md")


def load_agent_knowledge(agent_name: str) -> str:
    """
    Load and concatenate all three knowledge files for the given agent.
    agent_name: one of 'ba_agent', 'dev_agent', 'tech_lead_agent'
    Returns a single string to inject into the AI system message.
    """
    agent_dir = _KNOWLEDGE_DIR / agent_name
    parts: list[str] = []

    for filename in _FILES:
        path = agent_dir / filename
        if not path.exists():
            log.warning("Knowledge file missing: %s", path)
            continue
        try:
            content = path.read_text(encoding="utf-8").strip()
            if content:
                section = filename.replace(".md", "").replace("_", " ").title()
                parts.append(f"## {section}\n\n{content}")
        except OSError as exc:
            log.warning("Could not read knowledge file %s: %s", path, exc)

    if not parts:
        log.warning("No knowledge files found for agent '%s'", agent_name)
        return ""

    return "\n\n---\n\n".join(parts)
