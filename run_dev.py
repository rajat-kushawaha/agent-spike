"""
run_dev.py - Standalone Developer agent runner.

Polls every POLL_INTERVAL_SECONDS for tickets ready for implementation or fixes.
Multiple instances can run in parallel — each will pick up a different ticket
thanks to distributed locking.

Usage:
  python run_dev.py [--once] [--dry-run] [--log-level DEBUG|INFO|WARNING]
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time

from agents.dev_agent import DevAgent
from ai_client import AIClient
from config import load_config
from github_client import GitHubClient
from jira_client import JiraClient
from logger import configure_root_logger, get_logger
from slack_client import SlackClient
from state_manager import StateManager

log = get_logger(__name__)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Developer agent runner")
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
    agent = DevAgent(config, state, ai, jira, slack, github)
    state.recover_stuck_tasks()
    log.info("Dev agent started (pid=%d, dry_run=%s, once=%s)", os.getpid(), args.dry_run, args.once)

    if args.once:
        agent.run()
        return 0

    while True:
        try:
            processed = agent.run()
            if processed:
                log.info("Dev agent processed: %s", processed)
        except KeyboardInterrupt:
            log.info("Dev agent stopped")
            return 0
        except Exception as exc:
            log.error("Unexpected error in Dev cycle: %s", exc, exc_info=True)

        try:
            time.sleep(config.poll_interval_seconds)
        except KeyboardInterrupt:
            log.info("Dev agent stopped")
            return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
