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

import base64
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
        branch_name: str = task.get("branch_name", "")
        ba_analysis: dict = task.get("ba_analysis", {})

        # Build repo → pr_number map. Fall back to single pr_number on the primary repo key.
        pr_numbers: dict[str, int] = dict(task.get("pr_numbers") or {})
        if not pr_numbers and task.get("pr_number"):
            target_repos: list[str] = task.get("target_repos") or ["default"]
            pr_numbers = {target_repos[0]: task["pr_number"]}

        if not pr_numbers:
            log.warning("PR_RAISED task %s has no pr_numbers — skipping", ticket_id)
            return

        # Build path → repo_key map from BA plan so we can route submitted_files per repo
        path_to_repo: dict[str, str] = {
            entry["path"]: entry["repo"]
            for entry in ba_analysis.get("files_to_change", [])
            if entry.get("path") and entry.get("repo")
        }
        all_submitted = list(task.get("submitted_files", {}).keys())

        # Per-repo SHA dedup — stored as last_reviewed_shas: {repo_key: sha}
        last_reviewed_shas: dict[str, str] = dict(task.get("last_reviewed_shas") or {})

        prior_issues: list[str] = task.get("all_review_feedback", [])
        prior_feedback_text = (
            "\n".join(f"- {i}" for i in prior_issues[-30:])
            if prior_issues else "(none — this is the first review)"
        )

        all_decisions: list[str] = []
        all_new_issues: list[str] = []
        repos_needing_changes: list[str] = []

        # Mark as under review immediately — prevents duplicate concurrent reviews
        self._state.upsert_task(ticket_id, {"status": "UNDER_REVIEW"})

        for repo_key, pr_number in pr_numbers.items():
            gh = self._github.resolve_repo(repo_key)

            # Get current HEAD SHA for this repo's branch
            try:
                current_sha = gh.repo.get_branch(branch_name).commit.sha
            except Exception as exc:
                log.warning("Could not get branch SHA for %s/%s: %s", repo_key, branch_name, exc)
                current_sha = None

            # Skip if already reviewed this exact commit in this repo
            if current_sha and current_sha == last_reviewed_shas.get(repo_key):
                log.info(
                    "Tech lead skipping %s/%s — no new commits since last review (sha=%s)",
                    ticket_id, repo_key, current_sha[:8],
                )
                all_decisions.append("CHANGES_REQUESTED")  # treat as still pending
                continue

            log.info(
                "Tech lead reviewing PR #%d (%s) for %s (sha=%s)",
                pr_number, repo_key, ticket_id, (current_sha or "unknown")[:8],
            )

            # Record SHA before calling AI — prevents duplicate reviews if this takes time
            last_reviewed_shas[repo_key] = current_sha
            self._state.upsert_task(ticket_id, {"last_reviewed_shas": last_reviewed_shas})

            try:
                pr_obj = gh.repo.get_pull(pr_number)
                pr_title = pr_obj.title
            except Exception:
                pr_title = f"PR #{pr_number}"

            # Fetch only the files that belong to this repo
            repo_files = [p for p in all_submitted if path_to_repo.get(p) == repo_key]
            if repo_files:
                branch_files = self._fetch_branch_files(branch_name, repo_files, gh)
            else:
                # path_to_repo mapping incomplete — use the actual PR diff so we only see
                # what changed in this repo, not files from other repos
                log.warning("No path→repo mapping for %s/%s — using PR diff", ticket_id, repo_key)
                branch_files = gh.get_pr_diff_smart(pr_number)

            review = self._ai.complete(
                "tech_lead_review.txt",
                system_message=system_message,
                pr_number=str(pr_number),
                pr_title=pr_title,
                branch_name=branch_name,
                pr_diff=branch_files,
                ba_analysis=json.dumps(ba_analysis, indent=2),
                prior_feedback=prior_feedback_text,
            )

            if review.get("dry_run"):
                log.info("Dry-run: skipping tech lead review for %s/%s", ticket_id, repo_key)
                all_decisions.append("APPROVE")
                continue

            review = self._filter_spec_contradictions(review, ba_analysis, prior_issues)

            decision: str = review.get("decision", "REQUEST_CHANGES")
            if decision == "REQUEST_CHANGES" and not review.get("issues"):
                log.info("Tech lead: all issues filtered — auto-approving %s/%s", ticket_id, repo_key)
                decision = "APPROVE"
                review["decision"] = "APPROVE"
                review["summary"] = "All acceptance criteria met after filtering spec-contradicting issues."

            log.info("Tech lead decision for %s/%s: %s", ticket_id, repo_key, decision)
            all_decisions.append(decision)

            if decision == "APPROVE":
                self._approve_repo(ticket_id, pr_number, review, gh)
            else:
                self._request_changes_repo(ticket_id, pr_number, review, gh)
                repos_needing_changes.append(repo_key)
                all_new_issues.extend(review.get("issues", []))

        # Final status: only APPROVE the ticket when ALL repos approved
        if all(d == "APPROVE" for d in all_decisions):
            self._finalise_approval(ticket_id, pr_numbers, prior_issues, all_decisions)
        else:
            self._finalise_changes_requested(ticket_id, all_new_issues, prior_issues, task)

    def _fetch_branch_files(self, branch_name: str, paths: list[str], gh: GitHubClient | None = None) -> str:
        """Fetch complete file contents from the branch. No truncation."""
        repo = (gh or self._github).repo
        parts: list[str] = []
        seen: set[str] = set()
        for path in paths:
            if not path or path in seen:
                continue
            seen.add(path)
            try:
                contents = repo.get_contents(path, ref=branch_name)
                text = base64.b64decode(contents.content).decode("utf-8", errors="replace")  # type: ignore[union-attr]
                parts.append(f"### {path}\n```\n{text}\n```")
            except Exception as exc:
                log.debug("Could not fetch %s from %s: %s", path, branch_name, exc)
                parts.append(f"### {path}\n(could not fetch — file may have been removed)")
        result = "\n\n".join(parts)
        log.info("Fetched %d file(s) from branch %s (%d chars total)", len(seen), branch_name, len(result))
        return result

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

    def _approve_repo(self, ticket_id: str, pr_number: int, review: dict[str, Any], gh: GitHubClient) -> None:
        """Post an approval comment on one repo's PR. Final state update done in _finalise_approval."""
        summary = review.get("summary", "All criteria met.")
        log.info("Tech lead approving PR #%d for %s", pr_number, ticket_id)
        try:
            gh.approve_pull_request(pr_number, body=f"Approved by Tech Lead agent.\n\n{summary}")
        except Exception as exc:
            log.warning("GitHub approval failed for PR #%d: %s", pr_number, exc)

    def _request_changes_repo(self, ticket_id: str, pr_number: int, review: dict[str, Any], gh: GitHubClient) -> None:
        """Post a changes-requested comment on one repo's PR."""
        issues = review.get("issues", [])
        suggestions = review.get("suggestions", [])
        summary = review.get("summary", "")
        log.info("Tech lead requesting changes on PR #%d for %s (%d issue(s))", pr_number, ticket_id, len(issues))

        issues_text = "\n".join(f"  * {i}" for i in issues)
        suggestions_text = "\n".join(f"  * {s}" for s in suggestions)
        review_body = (
            f"[Tech Lead Review — Changes Requested]\n\n"
            f"Summary: {summary}\n\n"
            f"Issues to fix (all must be resolved before approval):\n{issues_text}"
            + (f"\n\nSuggestions (optional):\n{suggestions_text}" if suggestions else "")
        )
        try:
            gh.post_review_comment(pr_number, review_body)
        except Exception as exc:
            log.warning("GitHub review comment failed for PR #%d: %s", pr_number, exc)
        try:
            self._jira.add_comment(ticket_id, review_body)
        except Exception as exc:
            log.warning("Jira comment failed for %s: %s", ticket_id, exc)

    def _finalise_approval(
        self, ticket_id: str, pr_numbers: dict[str, int], prior_issues: list[str], all_decisions: list[str]
    ) -> None:
        """All repos approved — transition ticket to APPROVED."""
        self._state.set_status(ticket_id, "APPROVED")
        pr_summary = ", ".join(f"#{n} ({k})" for k, n in pr_numbers.items())
        try:
            self._jira.transition_issue(ticket_id, self._config.jira_status_ready_for_merge)
            self._jira.add_comment(
                ticket_id,
                f"All PRs approved by Tech Lead agent ({pr_summary}). Ready for manual merge.",
            )
        except Exception as exc:
            log.warning("Jira update failed after approval for %s: %s", ticket_id, exc)
        try:
            self._slack.post_task_update(
                ticket_id, "Tech Lead",
                f"All PRs APPROVED ({pr_summary}) — ready for manual merge.",
            )
        except Exception as exc:
            log.warning("Slack notification failed: %s", exc)

    def _finalise_changes_requested(
        self, ticket_id: str, new_issues: list[str], prior_issues: list[str], task: dict[str, Any]
    ) -> None:
        """At least one repo needs changes — accumulate feedback and re-queue for dev."""
        round_num = task.get("review_round", 0) + 1
        labelled = [f"[Round {round_num}] {issue}" for issue in new_issues]
        accumulated = prior_issues + labelled

        self._state.upsert_task(ticket_id, {
            "status": "CHANGES_REQUESTED",
            "review_feedback": accumulated,
            "all_review_feedback": accumulated,
            "review_round": round_num,
        })
        try:
            self._slack.post_task_update(
                ticket_id, "Tech Lead",
                f"{len(new_issues)} issue(s) across repos — re-queued for dev.",
            )
        except Exception as exc:
            log.warning("Slack notification failed: %s", exc)
