"""
ba_agent.py - Business Analyst agent.

Clarification flow (real Slack polling — no self-answering):
  1. Pick up Backlog tickets → analyse → if needs_clarification:
       a. Post questions to Slack as a threaded message, save thread_ts in state
       b. Set status BA_AWAITING_CLARIFICATION and STOP for this cycle
  2. On next cycle, pick up BA_AWAITING_CLARIFICATION tickets:
       a. Poll the Slack thread for human replies
       b. If no replies yet → log and skip (try again next cycle)
       c. If replies found → pass them to the AI clarification prompt,
          update Jira description, transition to In Progress, set BA_DESCRIPTION_UPDATED
  3. Tickets without clarification skip straight from BA_ANALYZING → BA_DESCRIPTION_UPDATED
"""
from __future__ import annotations

import json
from typing import Any

import console as ui
from ai_client import AIClient
from config import Config
from github_client import GitHubClient
from jira_client import JiraClient
from knowledge_loader import load_agent_knowledge
from logger import get_logger
from slack_client import SlackClient
from state_manager import StateManager

log = get_logger(__name__)

_BA_DONE_STATUSES = {
    "BA_DESCRIPTION_UPDATED", "IN_DEVELOPMENT", "PR_RAISED",
    "UNDER_REVIEW", "CHANGES_REQUESTED", "APPROVED", "FAILED",
    # legacy
    "BA_DONE", "DEV_PENDING", "DEV_DONE", "REVIEW_PENDING",
    "REVIEW_DONE", "FIX_PENDING", "MERGED",
}


class BAAgent:
    def __init__(
        self,
        config: Config,
        state: StateManager,
        ai: AIClient,
        jira: JiraClient,
        slack: SlackClient,
        github: GitHubClient | None = None,
    ) -> None:
        self._config = config
        self._state = state
        self._ai = ai
        self._jira = jira
        self._slack = slack
        self._github = github

    def run(self) -> list[str]:
        # Reload knowledge every cycle so live edits take effect immediately
        knowledge = load_agent_knowledge("ba_agent")
        system_message = (
            "You are a Senior Business Analyst on a software engineering team.\n\n"
            + knowledge
        )

        processed: list[str] = []

        # --- Phase 1: resume tickets waiting for Slack clarification ---
        for task_id, task in self._state.all_tasks().items():
            if task.get("status") != "BA_AWAITING_CLARIFICATION":
                continue
            if not self._state.claim_task(task_id, "ba_agent"):
                continue
            try:
                advanced = self._poll_clarification(task, system_message)
                if advanced:
                    processed.append(task_id)
            except Exception as exc:
                log.error("BA agent clarification poll failed on %s: %s", task_id, exc, exc_info=True)
            finally:
                self._state.release_task(task_id)

        # --- Phase 2: pick up fresh Backlog tickets ---
        try:
            issues = self._jira.get_backlog_issues()
        except Exception as exc:
            log.error("BA agent failed to fetch Backlog issues: %s", exc)
            return processed

        for issue in issues:
            ticket_id: str = issue["key"]
            current_status = self._state.get_status(ticket_id)

            if current_status in _BA_DONE_STATUSES or current_status == "BA_AWAITING_CLARIFICATION":
                log.debug("Skipping %s (status=%s)", ticket_id, current_status)
                ui.ba_skipped(ticket_id, f"already {current_status}")
                continue

            # Skip if already analysed — ba_analysis in state means BA work is done
            if self._state.get_task(ticket_id) and self._state.get_task(ticket_id).get("ba_analysis"):
                log.debug("Skipping %s — already has ba_analysis in state", ticket_id)
                ui.ba_skipped(ticket_id, "already analysed")
                continue

            if not self._state.claim_task(ticket_id, "ba_agent"):
                continue
            try:
                self._analyse_ticket(issue, system_message)
                processed.append(ticket_id)
            except Exception as exc:
                log.error("BA agent failed on %s: %s", ticket_id, exc, exc_info=True)
                ui.task_failed(ticket_id, "BA", exc)
                self._state.upsert_task(ticket_id, {"status": "FAILED", "error": str(exc)})
            finally:
                self._state.release_task(ticket_id)

        return processed

    # ------------------------------------------------------------------
    # Fresh ticket analysis
    # ------------------------------------------------------------------

    def _analyse_ticket(self, issue: dict[str, Any], system_message: str) -> None:
        ticket_id: str = issue["key"]
        fields: dict = issue.get("fields", {})
        summary: str = fields.get("summary", "")
        description: str = self._extract_description(fields.get("description"))
        comments: str = self._extract_comments(fields.get("comment"))
        labels: list[str] = fields.get("labels", [])

        log.info("BA agent analysing %s: %s", ticket_id, summary)
        ui.ba_analysing(ticket_id, summary)
        self._state.upsert_task(
            ticket_id,
            {
                "status": "BA_ANALYZING",
                "jira_summary": summary,
                "jira_description": description,
            },
        )

        available_repos = "\n".join(
            f"- {key}: {entry['repo']} (keywords: {', '.join(entry['keywords'])})"
            for key, entry in self._config.github_repos.items()
        )

        # Fetch actual directory structure from each repo so the AI generates correct paths
        repo_structures = self._fetch_repo_structures()

        analysis = self._ai.complete(
            "ba_analysis.txt",
            system_message=system_message,
            ticket_id=ticket_id,
            ticket_summary=summary,
            ticket_description=description or "(no description provided)",
            ticket_comments=comments or "(no comments)",
            ticket_labels=", ".join(labels) if labels else "(none)",
            available_repos=available_repos,
            repo_structures=repo_structures,
        )

        if analysis.get("dry_run"):
            log.info("Dry-run: skipping BA analysis for %s", ticket_id)
            self._state.upsert_task(
                ticket_id, {"status": "BA_DESCRIPTION_UPDATED", "ba_analysis": analysis}
            )
            return

        self._state.upsert_task(ticket_id, {"ba_analysis": analysis})

        if analysis.get("needs_clarification") and analysis.get("clarification_questions"):
            self._post_clarification_questions(ticket_id, summary, analysis)
        else:
            # No clarification needed — go straight to finalising
            self._finalise(ticket_id, summary, analysis, system_message)

    def _post_clarification_questions(
        self, ticket_id: str, summary: str, analysis: dict[str, Any]
    ) -> None:
        questions: list[str] = analysis["clarification_questions"]
        log.info(
            "BA agent needs clarification for %s — posting %d question(s) to Slack",
            ticket_id,
            len(questions),
        )
        ui.ba_clarification(ticket_id, len(questions))

        thread_ts = self._slack.post_clarification_request(ticket_id, summary, questions)

        if not thread_ts:
            log.error(
                "Failed to post clarification to Slack for %s — marking FAILED", ticket_id
            )
            self._state.upsert_task(
                ticket_id,
                {"status": "FAILED", "error": "Could not post clarification to Slack"},
            )
            return

        # Save thread_ts so the next cycle can poll it
        self._state.upsert_task(
            ticket_id,
            {
                "status": "BA_AWAITING_CLARIFICATION",
                "slack_thread_ts": thread_ts,
                "clarification_questions": questions,
            },
        )
        log.info(
            "BA agent waiting for Slack replies on %s (thread_ts=%s)", ticket_id, thread_ts
        )

    # ------------------------------------------------------------------
    # Clarification polling (called on subsequent cycles)
    # ------------------------------------------------------------------

    def _poll_clarification(self, task: dict[str, Any], system_message: str) -> bool:
        """
        Check the Slack thread for human replies.
        Returns True if clarification was received and the ticket advanced.
        Returns False if still waiting (no replies yet).
        """
        ticket_id: str = task["id"]
        thread_ts: str = task.get("slack_thread_ts", "")
        summary: str = task.get("jira_summary", "")
        questions: list[str] = task.get("clarification_questions", [])
        analysis: dict = task.get("ba_analysis", {})

        if not thread_ts:
            log.warning(
                "BA_AWAITING_CLARIFICATION task %s has no slack_thread_ts — cannot poll",
                ticket_id,
            )
            return False

        log.info("Polling Slack thread %s for clarification replies on %s", thread_ts, ticket_id)
        replies = self._slack.get_thread_replies(thread_ts)

        if not replies:
            log.info(
                "No replies yet in Slack thread for %s — will check again next cycle", ticket_id
            )
            ui.ba_waiting(ticket_id)
            return False

        # Collect all reply texts as the human's answers
        reply_texts = "\n".join(
            f"- {r.get('text', '').strip()}"
            for r in replies
            if r.get("text", "").strip()
        )

        log.info(
            "Received %d reply/replies for %s — processing clarification", len(replies), ticket_id
        )

        questions_text = "\n".join(f"- {q}" for q in questions)
        description: str = task.get("jira_description", "")

        clarification = self._ai.complete(
            "ba_clarification.txt",
            system_message=system_message,
            ticket_id=ticket_id,
            ticket_summary=summary,
            ticket_description=description or "(no description provided)",
            questions=questions_text,
            human_answers=reply_texts,
        )

        if not clarification.get("dry_run"):
            if clarification.get("updated_acceptance_criteria"):
                analysis["acceptance_criteria"] = clarification["updated_acceptance_criteria"]
            analysis["clarification_answers"] = clarification.get("answers", [])
            analysis["needs_clarification"] = False

        # Post a confirmation back in the same Slack thread
        try:
            self._slack.post_message(
                f"Clarification received for {ticket_id}. Proceeding with analysis.",
                thread_ts=thread_ts,
                use_clarification_channel=True,
            )
        except Exception as exc:
            log.warning("Could not post confirmation to Slack thread: %s", exc)

        self._finalise(ticket_id, summary, analysis, system_message)
        return True

    # ------------------------------------------------------------------
    # Shared finalisation (update Jira description + transition)
    # ------------------------------------------------------------------

    def _finalise(
        self,
        ticket_id: str,
        summary: str,
        analysis: dict[str, Any],
        system_message: str,
    ) -> None:
        enriched_description = self._build_enriched_description(summary, analysis)

        try:
            self._jira.append_to_description(ticket_id, enriched_description)
            log.info("Appended BA findings to Jira description for %s", ticket_id)
        except Exception as exc:
            log.warning("Could not append to Jira description for %s: %s", ticket_id, exc)

        try:
            self._jira.transition_issue(ticket_id, self._config.jira_status_in_progress)
        except Exception as exc:
            log.warning("Could not transition %s to In Progress: %s", ticket_id, exc)

        self._state.upsert_task(
            ticket_id,
            {
                "status": "BA_DESCRIPTION_UPDATED",
                "ba_analysis": analysis,
                "enriched_description": enriched_description,
            },
        )

        ui.ba_done(ticket_id, analysis.get("complexity", "unknown"))
        try:
            self._slack.post_task_update(
                ticket_id,
                "BA Complete",
                f"Description updated and ticket moved to In Progress for '{summary}' "
                f"(complexity: {analysis.get('complexity', 'unknown')})",
            )
        except Exception as exc:
            log.warning("Slack notification failed for %s: %s", ticket_id, exc)

        log.info("BA agent completed %s", ticket_id)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _fetch_repo_structures(self) -> str:
        """Fetch the directory tree for each configured repo to give the AI real path context."""
        if not self._github:
            return "(repo structures unavailable — no GitHub client)"
        parts: list[str] = []
        for key, entry in self._config.github_repos.items():
            try:
                gh = self._github.for_repo(entry["repo"])
                structure = gh.get_repo_structure(max_depth=2)
                parts.append(f"[{key}]\n{structure}")
            except Exception as exc:
                log.warning("Could not fetch structure for %s: %s", key, exc)
                parts.append(f"[{key}]\n(could not fetch — {exc})")
        return "\n\n".join(parts)

    @staticmethod
    def _extract_description(raw: Any) -> str:
        if not raw:
            return ""
        if isinstance(raw, str):
            return raw
        if isinstance(raw, dict):
            parts: list[str] = []

            def _walk(node: Any) -> None:
                if isinstance(node, dict):
                    if node.get("type") == "text":
                        parts.append(node.get("text", ""))
                    for child in node.get("content", []):
                        _walk(child)
                elif isinstance(node, list):
                    for item in node:
                        _walk(item)

            _walk(raw)
            return " ".join(parts)
        return str(raw)

    @staticmethod
    def _extract_comments(raw: Any) -> str:
        if not raw or not isinstance(raw, dict):
            return ""
        comments = raw.get("comments", [])
        if not comments:
            return ""
        parts: list[str] = []
        for c in comments:
            author = c.get("author", {}).get("displayName", "Unknown")
            body = BAAgent._extract_description(c.get("body", ""))
            if body:
                parts.append(f"{author}: {body}")
        return "\n".join(parts)

    @staticmethod
    def _build_enriched_description(summary: str, analysis: dict[str, Any]) -> str:
        lines: list[str] = [
            "[Enriched by BA Agent]\n",
            f"Summary: {analysis.get('summary', summary)}\n",
            f"Complexity: {analysis.get('complexity', 'unknown')}\n",
            "\nAcceptance Criteria:",
        ]
        for c in analysis.get("acceptance_criteria", []):
            lines.append(f"  - {c}")

        lines.append("\nFiles to Change:")
        for f in analysis.get("files_to_change", []):
            lines.append(f"  - {f['path']}: {f['reason']}")

        if analysis.get("function_signatures"):
            lines.append("\nFunction Signatures:")
            for sig in analysis["function_signatures"]:
                lines.append(f"  - {sig}")

        if analysis.get("edge_cases"):
            lines.append("\nEdge Cases:")
            for e in analysis["edge_cases"]:
                lines.append(f"  - {e}")

        if analysis.get("clarification_answers"):
            lines.append("\nClarification Q&A:")
            for qa in analysis["clarification_answers"]:
                lines.append(f"  Q: {qa.get('question', '')}")
                lines.append(f"  A: {qa.get('answer', '')}")

        if analysis.get("implementation_notes"):
            lines.append(f"\nImplementation Notes: {analysis['implementation_notes']}")

        return "\n".join(lines)
