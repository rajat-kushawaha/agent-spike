"""
config.py - Centralised configuration loader.
All environment variables are loaded once at import time so any missing
variable surfaces immediately rather than at the moment it is first used.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the project root (same directory as this file)
load_dotenv(Path(__file__).parent / ".env")


def _require(key: str) -> str:
    """Return env var value or raise with a clear message."""
    value = os.getenv(key)
    if not value:
        raise EnvironmentError(
            f"Required environment variable '{key}' is not set. "
            "Copy .env.example to .env and fill in your credentials."
        )
    return value


def _optional(key: str, default: str = "") -> str:
    return os.getenv(key, default)


@dataclass(frozen=True)
class Config:
    # OpenAI / OpenRouter
    openai_api_key: str
    openai_base_url: str
    openai_model: str

    # Jira
    jira_base_url: str
    jira_email: str
    jira_api_token: str
    jira_project_key: str

    # Slack
    slack_bot_token: str
    slack_channel_id: str
    slack_clarification_channel_id: str  # channel where BA posts clarification questions

    # GitHub
    github_token: str
    github_repos: dict        # name → {repo, keywords} mapping
    github_base_branch: str

    # Jira status names (configurable so boards with custom workflows work)
    jira_status_backlog: str
    jira_status_in_progress: str
    jira_status_in_review: str
    jira_status_ready_for_merge: str

    # Orchestrator behaviour
    poll_interval_seconds: int
    state_file: str


def _parse_github_repos() -> dict:
    """
    Parse GITHUB_REPOS env var into a structured dict.

    Format (semicolon-separated repos, pipe-separated fields):
      api:rajat-gitting/revelio-api:api,backend,auth,endpoint;ui:rajat-gitting/revelio-ui:ui,frontend,react,component

    Each entry: <key>:<owner/repo>:<comma-separated keywords>
    Keywords are optional — if omitted the key itself is used as the only keyword.

    Result:
      {
        "api": {"repo": "rajat-gitting/revelio-api", "keywords": ["api", "backend", "auth", "endpoint"]},
        "ui":  {"repo": "rajat-gitting/revelio-ui",  "keywords": ["ui", "frontend", "react", "component"]},
      }
    """
    raw = os.getenv("GITHUB_REPOS", "")
    repos: dict[str, dict] = {}
    if raw:
        for entry in raw.split(";"):
            entry = entry.strip()
            if not entry:
                continue
            parts = entry.split(":")
            if len(parts) < 2:
                continue
            key = parts[0].strip()
            repo = parts[1].strip()
            keywords = [k.strip() for k in parts[2].split(",")] if len(parts) > 2 else [key]
            repos[key] = {"repo": repo, "keywords": keywords}
    if not repos:
        raise EnvironmentError(
            "GITHUB_REPOS is required. Format: api:owner/repo:keyword1,keyword2;ui:owner/repo:keyword1,keyword2"
        )
    return repos


def load_config() -> Config:
    """Build and return a Config instance from environment variables."""
    return Config(
        openai_api_key=_require("OPENAI_API_KEY"),
        openai_base_url=_optional("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        openai_model=_optional("OPENAI_MODEL", "gpt-4o"),
        jira_base_url=_require("JIRA_BASE_URL"),
        jira_email=_require("JIRA_EMAIL"),
        jira_api_token=_require("JIRA_API_TOKEN"),
        jira_project_key=_optional("JIRA_PROJECT_KEY", "DEV"),
        slack_bot_token=_require("SLACK_BOT_TOKEN"),
        slack_channel_id=_require("SLACK_CHANNEL_ID"),
        slack_clarification_channel_id=_optional("SLACK_CLARIFICATION_CHANNEL_ID", ""),
        github_token=_require("GITHUB_TOKEN"),
        github_repos=_parse_github_repos(),
        github_base_branch=_optional("GITHUB_BASE_BRANCH", "main"),
        jira_status_backlog=_optional("JIRA_STATUS_BACKLOG", "Backlog"),
        jira_status_in_progress=_optional("JIRA_STATUS_IN_PROGRESS", "In Progress"),
        jira_status_in_review=_optional("JIRA_STATUS_IN_REVIEW", "In Review"),
        jira_status_ready_for_merge=_optional("JIRA_STATUS_READY_FOR_MERGE", "Ready for Merge"),
        poll_interval_seconds=int(_optional("POLL_INTERVAL_SECONDS", "60")),
        state_file=_optional("STATE_FILE", "state.json"),
    )
