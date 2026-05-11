"""
console.py - Rich-powered live output for the agent pipeline.

All agents call the functions here instead of plain log.info for any message
the demo audience needs to see. Structured, coloured, and readable at a glance.

Design:
  - Phase banners: clearly mark which agent is running
  - Ticket events: one line per meaningful action, prefixed with an icon
  - Section dividers for each orchestration cycle
  - Errors/warnings stand out in red/yellow
  - DEBUG log output (verbose API/GitHub details) stays in the plain logger
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from rich.console import Console
from rich.rule import Rule
from rich.text import Text
from rich.theme import Theme

_theme = Theme({
    "ba":        "bold cyan",
    "dev":       "bold blue",
    "tl":        "bold magenta",
    "orch":      "bold white",
    "ok":        "bold green",
    "warn":      "bold yellow",
    "err":       "bold red",
    "ticket":    "bold white",
    "dim":       "dim white",
    "step":      "white",
    "approve":   "bold green",
    "reject":    "bold yellow",
    "info":      "cyan",
})

console = Console(theme=_theme, highlight=False)


# ── Icons ────────────────────────────────────────────────────────────────────

_ICONS = {
    "think":   "🧠",
    "commit":  "📦",
    "pr":      "🔀",
    "review":  "🔍",
    "approve": "✅",
    "reject":  "🔁",
    "slack":   "💬",
    "jira":    "📋",
    "chunk":   "⚙️ ",
    "plan":    "📝",
    "fix":     "🔧",
    "skip":    "⏭ ",
    "lock":    "🔒",
    "done":    "✔ ",
    "fail":    "✗ ",
    "warn":    "⚠ ",
    "cycle":   "🔄",
    "start":   "▶ ",
    "repo":    "📁",
    "check":   "🩺",
}


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _icon(name: str) -> str:
    return _ICONS.get(name, "•")


# ── Cycle / orchestrator ─────────────────────────────────────────────────────

def cycle_start(cycle: int) -> None:
    console.print()
    console.rule(f"[orch]{_icon('cycle')} Cycle {cycle}  ·  {_ts()}[/orch]", style="white")


def cycle_sleep(seconds: int) -> None:
    console.print(f"  [dim]sleeping {seconds}s until next cycle…[/dim]")


def orchestrator_error(exc: Exception) -> None:
    console.print(f"  [{_ts()}] [err]{_icon('fail')} Orchestrator error:[/err] {exc}")


# ── Agent phase banners ───────────────────────────────────────────────────────

def agent_start(agent: str) -> None:
    labels = {"BA Agent": "ba", "Dev Agent": "dev", "Tech Lead": "tl"}
    style = labels.get(agent, "orch")
    console.print(f"\n  [{style}]── {agent} ──[/{style}]")


# ── BA Agent ─────────────────────────────────────────────────────────────────

def ba_analysing(ticket_id: str, summary: str) -> None:
    console.print(f"  {_ts()}  [ba]{_icon('think')} BA[/ba]  [ticket]{ticket_id}[/ticket]  analysing: [step]{summary[:60]}[/step]")


def ba_clarification(ticket_id: str, n: int) -> None:
    console.print(f"  {_ts()}  [ba]{_icon('slack')} BA[/ba]  [ticket]{ticket_id}[/ticket]  posted {n} clarification question(s) to Slack")


def ba_waiting(ticket_id: str) -> None:
    console.print(f"  {_ts()}  [ba]{_icon('skip')} BA[/ba]  [ticket]{ticket_id}[/ticket]  [dim]waiting for Slack reply…[/dim]")


def ba_done(ticket_id: str, complexity: str) -> None:
    console.print(f"  {_ts()}  [ba]{_icon('done')} BA[/ba]  [ticket]{ticket_id}[/ticket]  [ok]complete[/ok]  complexity: {complexity}")


def ba_skipped(ticket_id: str, reason: str) -> None:
    console.print(f"  {_ts()}  [ba]{_icon('skip')} BA[/ba]  [ticket]{ticket_id}[/ticket]  [dim]skip — {reason}[/dim]")


# ── Dev Agent ────────────────────────────────────────────────────────────────

def dev_planning(ticket_id: str) -> None:
    console.print(f"  {_ts()}  [dev]{_icon('plan')} Dev[/dev]  [ticket]{ticket_id}[/ticket]  planning implementation chunks…")


def dev_chunk(ticket_id: str, i: int, total: int, desc: str, repo: str) -> None:
    console.print(
        f"  {_ts()}  [dev]{_icon('chunk')} Dev[/dev]  [ticket]{ticket_id}[/ticket]  "
        f"chunk {i}/{total}: [step]{desc[:50]}[/step]  [dim]→ {repo}[/dim]"
    )


def dev_committed(ticket_id: str, i: int, total: int, repo: str, files: list[str]) -> None:
    names = ", ".join(f.split("/")[-1] for f in files[:3])
    extra = f" +{len(files) - 3} more" if len(files) > 3 else ""
    console.print(
        f"  {_ts()}  [dev]{_icon('commit')} Dev[/dev]  [ticket]{ticket_id}[/ticket]  "
        f"[ok]committed[/ok] chunk {i}/{total}  [dim]{names}{extra} → {repo}[/dim]"
    )


def dev_self_check(ticket_id: str) -> None:
    console.print(f"  {_ts()}  [dev]{_icon('check')} Dev[/dev]  [ticket]{ticket_id}[/ticket]  running self-check against all ACs…")


def dev_self_check_gap(ticket_id: str, n: int) -> None:
    console.print(f"  {_ts()}  [dev]{_icon('warn')} Dev[/dev]  [ticket]{ticket_id}[/ticket]  [warn]{n} AC(s) not covered — implementing remainder[/warn]")


def dev_pr_raised(ticket_id: str, pr_summaries: str) -> None:
    console.print(
        f"  {_ts()}  [dev]{_icon('pr')} Dev[/dev]  [ticket]{ticket_id}[/ticket]  "
        f"[ok]PR raised[/ok]  {pr_summaries}"
    )


def dev_fixing(ticket_id: str, n: int) -> None:
    console.print(
        f"  {_ts()}  [dev]{_icon('fix')} Dev[/dev]  [ticket]{ticket_id}[/ticket]  "
        f"fixing {n} Tech Lead issue(s)…"
    )


def dev_fix_committed(ticket_id: str, repo: str) -> None:
    console.print(
        f"  {_ts()}  [dev]{_icon('commit')} Dev[/dev]  [ticket]{ticket_id}[/ticket]  "
        f"[ok]fix committed[/ok]  [dim]→ {repo}[/dim]"
    )


def dev_skipped(ticket_id: str, reason: str) -> None:
    console.print(f"  {_ts()}  [dev]{_icon('skip')} Dev[/dev]  [ticket]{ticket_id}[/ticket]  [dim]skip — {reason}[/dim]")


# ── Tech Lead Agent ───────────────────────────────────────────────────────────

def tl_reviewing(ticket_id: str, pr_number: int, repo: str) -> None:
    console.print(
        f"  {_ts()}  [tl]{_icon('review')} TL[/tl]   [ticket]{ticket_id}[/ticket]  "
        f"reviewing PR #{pr_number}  [dim]({repo})[/dim]"
    )


def tl_approved(ticket_id: str, pr_number: int, repo: str) -> None:
    console.print(
        f"  {_ts()}  [tl]{_icon('approve')} TL[/tl]   [ticket]{ticket_id}[/ticket]  "
        f"[approve]APPROVED[/approve]  PR #{pr_number}  [dim]({repo})[/dim]"
    )


def tl_changes(ticket_id: str, pr_number: int, repo: str, n: int) -> None:
    console.print(
        f"  {_ts()}  [tl]{_icon('reject')} TL[/tl]   [ticket]{ticket_id}[/ticket]  "
        f"[reject]REQUEST CHANGES[/reject]  PR #{pr_number}  [dim]({repo})[/dim]  {n} issue(s)"
    )


def tl_issue(issue: str) -> None:
    console.print(f"             [dim]  · {issue[:90]}[/dim]")


def tl_all_approved(ticket_id: str) -> None:
    console.print(
        f"  {_ts()}  [tl]{_icon('approve')} TL[/tl]   [ticket]{ticket_id}[/ticket]  "
        f"[approve]ALL REPOS APPROVED — ready for manual merge[/approve]"
    )


def tl_skipped_no_new_commits(ticket_id: str, repo: str) -> None:
    console.print(
        f"  {_ts()}  [tl]{_icon('skip')} TL[/tl]   [ticket]{ticket_id}[/ticket]  "
        f"[dim]skip {repo} — no new commits since last review[/dim]"
    )


def tl_missing_files(ticket_id: str, repo: str, files: list[str]) -> None:
    console.print(
        f"  {_ts()}  [tl]{_icon('warn')} TL[/tl]   [ticket]{ticket_id}[/ticket]  "
        f"[warn]{len(files)} planned file(s) missing from {repo}[/warn]"
    )
    for f in files:
        console.print(f"             [warn]  · {f}[/warn]")


# ── Generic error / warning (all agents) ─────────────────────────────────────

def task_failed(ticket_id: str, agent: str, exc: Exception) -> None:
    console.print(
        f"  {_ts()}  [err]{_icon('fail')} {agent}[/err]  [ticket]{ticket_id}[/ticket]  "
        f"[err]FAILED:[/err] {str(exc)[:120]}"
    )


def warn(ticket_id: str, agent: str, msg: str) -> None:
    console.print(f"  {_ts()}  [warn]{_icon('warn')} {agent}[/warn]  [ticket]{ticket_id}[/ticket]  [warn]{msg[:100]}[/warn]")


# ── Rich logging handler (replaces plain StreamHandler for INFO+) ─────────────

class RichLoggingHandler(logging.Handler):
    """
    Routes WARNING and ERROR records from the standard logger to the rich console
    so library warnings (PyGithub, requests, etc.) still appear but in colour.
    DEBUG and INFO from our own code are handled by the console.* calls above —
    this handler only catches things we didn't explicitly surface.
    """

    def emit(self, record: logging.LogRecord) -> None:
        msg = self.format(record)
        # Only show library-level noise that isn't already surfaced by console.*
        if record.name.startswith("agents.") or record.name in (
            "ba_agent", "dev_agent", "tech_lead_agent",
            "orchestrator", "ai_client", "github_client",
            "jira_client", "slack_client", "state_manager",
        ):
            # Our own modules: only show WARNING and above (INFO handled by console.*)
            if record.levelno >= logging.WARNING:
                style = "err" if record.levelno >= logging.ERROR else "warn"
                console.print(f"  [dim]{record.created:.0f}[/dim]  [{style}]{msg}[/{style}]")
        else:
            # Third-party libraries: show WARNING+
            if record.levelno >= logging.WARNING:
                console.print(f"  [dim]{msg}[/dim]")
