"""
orchestrator.py - Main entry point for the multi-agent workflow system.

Usage:
  python orchestrator.py [--dry-run] [--only-agent <ba|dev|tech_lead>] [--once]

Options:
  --dry-run          Log all external API calls without executing them
  --only-agent NAME  Run only the specified agent (ba, dev, or tech_lead)
  --once             Run one cycle and exit (default: loop forever)
  --log-level LEVEL  Set log level (DEBUG, INFO, WARNING, ERROR). Default: INFO
"""
from __future__ import annotations

import argparse
import logging
import sys
import time

import console as ui
from agents.ba_agent import BAAgent
from agents.dev_agent import DevAgent
from agents.tech_lead_agent import TechLeadAgent
from ai_client import AIClient
from config import load_config
from github_client import GitHubClient
from jira_client import JiraClient
from logger import configure_root_logger, get_logger
from slack_client import SlackClient
from state_manager import StateManager

log = get_logger(__name__)

_cycle = 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Multi-agent software development workflow orchestrator"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Log external API calls without executing them")
    parser.add_argument("--only-agent", choices=["ba", "dev", "tech_lead"], default=None,
                        help="Run only one agent per cycle for phased testing")
    parser.add_argument("--once", action="store_true",
                        help="Run a single cycle and exit instead of looping forever")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        default="INFO", help="Logging verbosity level")
    return parser.parse_args()


def run_cycle(
    ba: BAAgent,
    dev: DevAgent,
    tl: TechLeadAgent,
    only_agent: str | None,
    dry_run: bool,
) -> None:
    """Execute one orchestration cycle — BA → Dev → Tech Lead (or just one agent).

    Dev and Tech Lead are intentionally separated: if Dev pushes to a PR this
    cycle, Tech Lead will NOT review it until the next cycle. This gives the
    full push time to land on GitHub and prevents the Tech Lead from reviewing
    a stale diff or re-reviewing work it already saw.
    """
    global _cycle
    _cycle += 1
    ui.cycle_start(_cycle)

    if only_agent is None or only_agent == "ba":
        ui.agent_start("BA Agent")
        ba.run()

    just_pushed: set[str] = set()
    if only_agent is None or only_agent == "dev":
        ui.agent_start("Dev Agent")
        processed = dev.run()
        just_pushed = set(processed)

    if only_agent is None or only_agent == "tech_lead":
        ui.agent_start("Tech Lead")
        tl.run(skip_tickets=just_pushed)


def main() -> int:
    args = parse_args()
    log_level = getattr(logging, args.log_level)
    configure_root_logger(level=log_level)

    from rich.panel import Panel
    from console import console
    console.print(Panel(
        "[bold white]Agent Spike — AI Engineering Pipeline[/bold white]\n"
        f"[dim]model: anthropic/claude-sonnet-4-5  ·  dry_run={args.dry_run}"
        + (f"  ·  agent={args.only_agent}" if args.only_agent else "") + "[/dim]",
        border_style="white",
        padding=(0, 2),
    ))

    try:
        config = load_config()
    except EnvironmentError as exc:
        log.error("Configuration error: %s", exc)
        return 1

    state = StateManager(config.state_file)
    ai = AIClient(config, dry_run=args.dry_run)
    jira = JiraClient(config, dry_run=args.dry_run)
    slack = SlackClient(config, dry_run=args.dry_run)
    github = GitHubClient(config, dry_run=args.dry_run)

    ba = BAAgent(config, state, ai, jira, slack, github)
    dev = DevAgent(config, state, ai, jira, slack, github)
    tl = TechLeadAgent(config, state, ai, jira, slack, github)

    if args.once:
        run_cycle(ba, dev, tl, args.only_agent, args.dry_run)
        return 0

    while True:
        try:
            run_cycle(ba, dev, tl, args.only_agent, args.dry_run)
        except KeyboardInterrupt:
            console.print("\n[dim]Orchestrator stopped.[/dim]")
            return 0
        except Exception as exc:
            ui.orchestrator_error(exc)
            log.error("Cycle error", exc_info=True)

        ui.cycle_sleep(config.poll_interval_seconds)
        try:
            time.sleep(config.poll_interval_seconds)
        except KeyboardInterrupt:
            console.print("\n[dim]Orchestrator stopped.[/dim]")
            return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
