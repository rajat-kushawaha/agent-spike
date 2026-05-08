"""
tech_lead_agent.py - Tech Lead agent.

New workflow:
  1. Pick up PR_RAISED tickets
  2. Fetch the PR diff from GitHub
  3. Call AI to review against BA acceptance criteria + knowledge rules
  4. APPROVE → call GitHub review approval API, update Jira to Ready for Merge, stop
  5. REQUEST_CHANGES → post review comment on PR, set CHANGES_REQUESTED for Dev agent
  6. Loop is unlimited — no max review cycle cap
  7. Never merges — human merges manually
"""
from __future__ import annotations

import json
import re
from typing import Any

from ai_client import AIClient
from config import Config
from github_client import GitHubClient
from jira_client import JiraClient
from knowledge_loader import load_agent_knowledge
from logger import get_logger
from slack_client import SlackClient
from state_manager import StateManager

log = get_logger(__name__)


class TechLeadAgent:
    def __init__(
        self,
        config: Config,
        state: StateManager,
        ai: AIClient,
        jira: JiraClient,
        slack: SlackClient,
        github: GitHubClient,
    ) -> None:
        self._config = config
        self._state = state
        self._ai = ai
        self._jira = jira
        self._slack = slack
        self._github = github

    def run(self, skip_tickets: set[str] | None = None) -> list[str]:
        knowledge = load_agent_knowledge("tech_lead_agent")
        system_message = (
            "You are a Tech Lead responsible for reviewing pull requests. "
            "Apply the rules and standards below strictly.\n\n"
            + knowledge
        )

        skip = skip_tickets or set()
        processed: list[str] = []
        for task_id, task in self._state.all_tasks().items():
            if task.get("status") != "PR_RAISED":
                continue
            if task_id in skip:
                log.info(
                    "Tech Lead skipping %s this cycle — Dev just pushed, will review next cycle",
                    task_id,
                )
                continue
            if not self._state.claim_task(task_id, "tech_lead_agent"):
                continue
            try:
                self._review_task(task, system_message)
                processed.append(task_id)
            except Exception as exc:
                log.error("Tech lead agent failed on %s: %s", task_id, exc, exc_info=True)
                self._state.upsert_task(task_id, {"status": "FAILED", "error": str(exc)})
            finally:
                self._state.release_task(task_id)
        return processed

    def _review_task(self, task: dict[str, Any], system_message: str) -> None:
        ticket_id: str = task["id"]
        pr_number: int = task.get("pr_number", 0)
        branch_name: str = task.get("branch_name", "")
        ba_analysis: dict = task.get("ba_analysis", {})

        if not pr_number:
            log.warning("PR_RAISED task %s has no pr_number — skipping", ticket_id)
            return

        # Check the current HEAD commit on the branch
        try:
            current_sha = self._github.repo.get_branch(branch_name).commit.sha
        except Exception as exc:
            log.warning("Could not get branch SHA for %s: %s", branch_name, exc)
            current_sha = None

        # Skip if Tech Lead already reviewed this exact commit — Dev hasn't pushed anything new yet
        last_reviewed_sha = task.get("last_reviewed_sha")
        if current_sha and current_sha == last_reviewed_sha:
            log.info(
                "Tech lead skipping %s — no new commits since last review (sha=%s)",
                ticket_id, current_sha[:8],
            )
            # Put it back to CHANGES_REQUESTED so Dev picks it up next cycle
            self._state.set_status(ticket_id, "CHANGES_REQUESTED")
            return

        log.info("Tech lead reviewing PR #%d for %s (sha=%s)", pr_number, ticket_id, (current_sha or "unknown")[:8])
        # Record SHA immediately — prevents another instance reviewing the same commit
        # even if this review takes a long time or fails partway through
        self._state.upsert_task(ticket_id, {"status": "UNDER_REVIEW", "last_reviewed_sha": current_sha})

        pr_diff = self._github.get_pr_diff(pr_number)

        try:
            pr_obj = self._github.repo.get_pull(pr_number)
            pr_title = pr_obj.title
        except Exception:
            pr_title = f"PR #{pr_number}"

        prior_issues: list[str] = task.get("all_review_feedback", [])
        prior_feedback_text = (
            "\n".join(f"- {i}" for i in prior_issues[-30:])  # last 30 to stay within token budget
            if prior_issues else "(none — this is the first review)"
        )

        review = self._ai.complete(
            "tech_lead_review.txt",
            system_message=system_message,
            pr_number=str(pr_number),
            pr_title=pr_title,
            branch_name=branch_name,
            pr_diff=pr_diff[:16000],
            ba_analysis=json.dumps(ba_analysis, indent=2),
            prior_feedback=prior_feedback_text,
        )

        if review.get("dry_run"):
            log.info("Dry-run: skipping tech lead review for %s", ticket_id)
            self._state.upsert_task(ticket_id, {"status": "APPROVED", "review": review})
            return

        # Strip issues that contradict explicit BA spec decisions or have been raised too many times
        review = self._filter_spec_contradictions(review, ba_analysis, prior_issues)

        decision: str = review.get("decision", "REQUEST_CHANGES")
        # If all issues were filtered out, flip to APPROVE
        if decision == "REQUEST_CHANGES" and not review.get("issues"):
            log.info("Tech lead: all issues were spec contradictions — auto-approving %s", ticket_id)
            review["decision"] = "APPROVE"
            review["summary"] = "All acceptance criteria met after filtering spec-contradicting issues."
            decision = "APPROVE"

        log.info("Tech lead decision for %s: %s", ticket_id, decision)
        self._state.upsert_task(ticket_id, {"review": review})

        if decision == "APPROVE":
            self._approve(ticket_id, pr_number, review)
        else:
            self._request_changes(ticket_id, pr_number, review)

    def _filter_spec_contradictions(self, review: dict[str, Any], ba_analysis: dict, prior_issues: list[str]) -> dict[str, Any]:
        """Remove issues that contradict the BA spec or have been raised too many times."""
        ac_text = " ".join(ba_analysis.get("acceptance_criteria", [])).lower()
        notes_text = ba_analysis.get("implementation_notes", "").lower()
        spec_text = ac_text + " " + notes_text

        # Build a set of keywords that indicate the spec explicitly chose a storage approach
        spec_storage_keywords: list[str] = []
        if any(term in spec_text for term in [".json", "json file", "users.json", "submissions.json"]):
            spec_storage_keywords += ["json file", "json", "file-based", "file storage", "flat file"]
        if "sqlite" in spec_text:
            spec_storage_keywords += ["sqlite"]
        if "in-memory" in spec_text or "in memory" in spec_text:
            spec_storage_keywords += ["in-memory", "in memory"]

        # Patterns that indicate the issue is challenging the spec's storage choice
        contradiction_patterns = [
            r"consider using a database",
            r"use a database",
            r"switch to a database",
            r"database for (user|storing|storage)",
            r"not secure.*json",
            r"json.*not secure",
            r"file.based storage.*security",
            r"security.*file.based storage",
            r"multi.step form logic.*backend",
            r"backend.*multi.step form logic",
        ]

        # Count how many times each prior issue has appeared (by first 60 chars as fingerprint)
        prior_fingerprints: dict[str, int] = {}
        for p in prior_issues:
            key = re.sub(r"^\[Round \d+\]\s*", "", p).strip().lower()[:60]
            prior_fingerprints[key] = prior_fingerprints.get(key, 0) + 1

        issues: list[str] = review.get("issues", [])
        filtered: list[str] = []
        moved_to_suggestions: list[str] = []

        for issue in issues:
            issue_lower = issue.lower()

            # Filter spec contradictions
            is_contradiction = any(re.search(p, issue_lower) for p in contradiction_patterns)
            if is_contradiction and (spec_storage_keywords or "multi-step" in spec_text or "multi_step" in spec_text):
                log.info("Filtering spec-contradicting issue: %s", issue[:80])
                moved_to_suggestions.append(f"[Spec decision — not blocking] {issue}")
                continue

            # Demote issues raised 3+ times without resolution
            fingerprint = issue_lower[:60]
            repeat_count = prior_fingerprints.get(fingerprint, 0)
            if repeat_count >= 3:
                log.info("Demoting repeated issue (seen %dx): %s", repeat_count, issue[:80])
                moved_to_suggestions.append(f"[Repeated {repeat_count}x — escalate to human] {issue}")
                continue

            filtered.append(issue)

        if moved_to_suggestions:
            review = dict(review)
            review["issues"] = filtered
            review["suggestions"] = review.get("suggestions", []) + moved_to_suggestions

        return review

    def _approve(
        self, ticket_id: str, pr_number: int, review: dict[str, Any]
    ) -> None:
        summary = review.get("summary", "All criteria met.")
        log.info("Tech lead approving PR #%d for %s", pr_number, ticket_id)

        # Approve via GitHub review API — does NOT merge
        self._github.approve_pull_request(
            pr_number,
            body=f"Approved by Tech Lead agent.\n\n{summary}",
        )

        self._state.set_status(ticket_id, "APPROVED")

        # Transition Jira to Ready for Merge
        try:
            self._jira.transition_issue(
                ticket_id, self._config.jira_status_ready_for_merge
            )
            self._jira.add_comment(
                ticket_id,
                f"PR #{pr_number} approved by Tech Lead agent. Ready for manual merge.\n\n"
                f"Review summary: {summary}",
            )
        except Exception as exc:
            log.warning("Jira update failed after approval for %s: %s", ticket_id, exc)

        try:
            self._slack.post_task_update(
                ticket_id,
                "Tech Lead",
                f"PR #{pr_number} APPROVED — ready for manual merge. {summary}",
            )
        except Exception as exc:
            log.warning("Slack notification failed: %s", exc)

    def _request_changes(
        self, ticket_id: str, pr_number: int, review: dict[str, Any]
    ) -> None:
        issues = review.get("issues", [])
        suggestions = review.get("suggestions", [])
        summary = review.get("summary", "")

        log.info(
            "Tech lead requesting changes on PR #%d for %s (%d issue(s))",
            pr_number,
            ticket_id,
            len(issues),
        )

        issues_text = "\n".join(f"  * {i}" for i in issues)
        suggestions_text = "\n".join(f"  * {s}" for s in suggestions)
        review_body = (
            f"[Tech Lead Review — Changes Requested]\n\n"
            f"Summary: {summary}\n\n"
            f"Issues to fix (all must be resolved before approval):\n{issues_text}"
            + (f"\n\nSuggestions (optional):\n{suggestions_text}" if suggestions else "")
        )

        # Post as a GitHub REQUEST_CHANGES review so the developer sees it in the PR
        self._github.post_review_comment(pr_number, review_body)

        # Also post to Jira for traceability
        try:
            self._jira.add_comment(ticket_id, review_body)
        except Exception as exc:
            log.warning("Jira comment failed for %s: %s", ticket_id, exc)

        # Accumulate all feedback across review rounds so the Dev agent sees the full history
        task = self._state.get_task(ticket_id) or {}
        previous_feedback: list[str] = task.get("all_review_feedback", [])
        # Prefix each new issue with the round number so Dev knows what's new vs old
        round_num = task.get("review_round", 0) + 1
        labelled_issues = [f"[Round {round_num}] {issue}" for issue in issues]
        accumulated_feedback = previous_feedback + labelled_issues

        self._state.upsert_task(
            ticket_id,
            {
                "status": "CHANGES_REQUESTED",
                "review_feedback": accumulated_feedback,
                "all_review_feedback": accumulated_feedback,
                "review_round": round_num,
            },
        )

        try:
            self._slack.post_task_update(
                ticket_id,
                "Tech Lead",
                f"PR #{pr_number} needs changes — {len(issues)} issue(s). Re-queued for dev.",
            )
        except Exception as exc:
            log.warning("Slack notification failed: %s", exc)
