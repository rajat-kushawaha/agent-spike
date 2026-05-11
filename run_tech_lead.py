"""
run_tech_lead.py - Standalone Tech Lead agent runner.

Usage:
  python run_tech_lead.py [--once] [--dry-run] [--log-level DEBUG|INFO|WARNING]
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time

import console as ui
from agents.tech_lead_agent import TechLeadAgent
from ai_client import AIClient
from config import load_config
from github_client import GitHubClient
from jira_client import JiraClient
from logger import configure_root_logger, get_logger
from slack_client import SlackClient
from state_manager import StateManager

log = get_logger(__name__)

_LABEL = "Tech Lead"
_STYLE = "tl"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Tech Lead agent runner")
    p.add_argument("--once", action="store_true", help="Run one cycle and exit")
    p.add_argument("--dry-run", action="store_true", help="Log API calls without executing")
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p.parse_args()


def main() -> int:
    args = parse_args()
    configure_root_logger(level=getattr(logging, args.log_level))

    try:
        config = load_config()
    except EnvironmentError as exc:
        log.error("Config error: %s", exc)
        return 1

    state = StateManager(config.state_file)
    ai = AIClient(config, dry_run=args.dry_run)
    jira = JiraClient(config, dry_run=args.dry_run)
    slack = SlackClient(config, dry_run=args.dry_run)
    github = GitHubClient(config, dry_run=args.dry_run)
    agent = TechLeadAgent(config, state, ai, jira, slack, github)
    state.recover_stuck_tasks()

    ui.startup(_LABEL, config.openai_model, os.getpid(), args.dry_run, config.poll_interval_seconds)

    cycle = 0
    if args.once:
        cycle += 1
        ui.agent_cycle_start(_LABEL, _STYLE, cycle)
        ui.agent_polling(_LABEL, _STYLE)
        agent.run()
        return 0

    while True:
        cycle += 1
        ui.agent_cycle_start(_LABEL, _STYLE, cycle)
        ui.agent_polling(_LABEL, _STYLE)
        try:
            processed = agent.run()
            if not processed:
                ui.agent_nothing_to_do(_LABEL, _STYLE)
        except KeyboardInterrupt:
            ui.console.print("\n[dim]Tech Lead stopped.[/dim]")
            return 0
        except Exception as exc:
            log.error("Unexpected error in Tech Lead cycle: %s", exc, exc_info=True)

        ui.agent_idle(_LABEL, config.poll_interval_seconds)
        try:
            time.sleep(config.poll_interval_seconds)
        except KeyboardInterrupt:
            ui.console.print("\n[dim]Tech Lead stopped.[/dim]")
            return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
