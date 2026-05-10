"""
run_ba.py - Standalone BA agent runner.

Polls every POLL_INTERVAL_SECONDS for Backlog tickets and clarification replies.
Multiple instances can run in parallel — distributed locking ensures no two
instances process the same ticket simultaneously.

Usage:
  python run_ba.py [--once] [--dry-run] [--log-level DEBUG|INFO|WARNING]
"""
from __future__ import annotations

import argparse
import logging
import sys
import time

from agents.ba_agent import BAAgent
from ai_client import AIClient
from config import load_config
from jira_client import JiraClient
from logger import configure_root_logger, get_logger
from slack_client import SlackClient
from state_manager import StateManager

log = get_logger(__name__)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="BA agent runner")
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
    agent = BAAgent(config, state, ai, jira, slack)
    state.recover_stuck_tasks()
    log.info("BA agent started (pid=%d, dry_run=%s, once=%s)", __import__("os").getpid(), args.dry_run, args.once)

    if args.once:
        agent.run()
        return 0

    while True:
        try:
            processed = agent.run()
            if processed:
                log.info("BA agent processed: %s", processed)
        except KeyboardInterrupt:
            log.info("BA agent stopped")
            return 0
        except Exception as exc:
            log.error("Unexpected error in BA cycle: %s", exc, exc_info=True)

        try:
            time.sleep(config.poll_interval_seconds)
        except KeyboardInterrupt:
            log.info("BA agent stopped")
            return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
