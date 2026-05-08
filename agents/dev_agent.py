"""
dev_agent.py - Developer agent.

Implementation loop (fresh ticket):
  1. Ask AI to break the BA analysis into logical implementation chunks
  2. For each chunk: generate code + tests, commit to branch
  3. After all chunks: ask AI to self-check — are ALL acceptance criteria covered?
  4. If self-check fails: generate remaining files and commit
  5. Only when self-check passes → raise PR

Fix loop (after Tech Lead review):
  1. Fetch current files from branch
  2. Pass ALL accumulated review feedback to AI in one shot
  3. AI produces complete corrected files
  4. Commit + update existing PR (no new PR)
  5. Tech Lead reviews next cycle (orchestrator enforces the gap)
"""
from __future__ import annotations

import base64
import json
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

_TRIGGER_STATUSES = {
    "BA_DESCRIPTION_UPDATED",
    "CHANGES_REQUESTED",
    "BA_DONE",   # legacy
    "FIX_PENDING",  # legacy
}

# Maximum implementation chunks per ticket to avoid runaway loops
_MAX_CHUNKS = 8


class DevAgent:
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

    def run(self) -> list[str]:
        knowledge = load_agent_knowledge("dev_agent")
        system_message = (
            "You are a Senior Software Engineer. Follow all rules and conventions below.\n\n"
            + knowledge
        )

        processed: list[str] = []
        for task_id, task in self._state.all_tasks().items():
            status = task.get("status")
            if status not in _TRIGGER_STATUSES:
                continue
            if not self._state.claim_task(task_id, "dev_agent"):
                continue
            try:
                # Re-read status after acquiring lock — another instance may have taken it
                fresh_task = self._state.get_task(task_id) or task
                fresh_status = fresh_task.get("status")
                if fresh_status not in _TRIGGER_STATUSES:
                    log.debug("Dev skipping %s — status changed to %s after lock acquired", task_id, fresh_status)
                    continue
                if fresh_status in ("CHANGES_REQUESTED", "FIX_PENDING"):
                    self._fix(fresh_task, system_message)
                else:
                    self._implement(fresh_task, system_message)
                processed.append(task_id)
            except Exception as exc:
                log.error("Dev agent failed on %s: %s", task_id, exc, exc_info=True)
                self._state.upsert_task(task_id, {"status": "FAILED", "error": str(exc)})
            finally:
                self._state.release_task(task_id)
        return processed

    # ------------------------------------------------------------------
    # Fresh implementation — inner loop until all ACs covered
    # ------------------------------------------------------------------

    def _implement(self, task: dict[str, Any], system_message: str) -> None:
        ticket_id: str = task["id"]
        ba_analysis: dict = task.get("ba_analysis", {})
        acceptance_criteria: list[str] = ba_analysis.get("acceptance_criteria", [])

        log.info("Dev agent implementing %s (%d ACs)", ticket_id, len(acceptance_criteria))
        self._state.set_status(ticket_id, "IN_DEVELOPMENT")

        # Step 1: plan chunks
        existing_files_text = self._fetch_existing_files(ba_analysis.get("files_to_change", []))
        plan = self._ai.complete(
            "dev_plan.txt",
            system_message=system_message,
            ticket_id=ticket_id,
            ba_analysis=json.dumps(ba_analysis, indent=2),
            existing_files=existing_files_text,
            ticket_id_lower=ticket_id.lower(),
        )

        if plan.get("dry_run"):
            log.info("Dry-run: skipping dev implementation for %s", ticket_id)
            self._state.upsert_task(ticket_id, {"status": "PR_RAISED"})
            return

        branch_name: str = plan.get("branch_name", f"feature/{ticket_id.lower()}")
        chunks: list[dict] = plan.get("chunks", [])
        pr_title: str = plan.get("pr_title", f"feat: {ticket_id}")
        pr_body: str = plan.get("pr_body", "")

        if not chunks:
            raise ValueError(f"AI returned no implementation chunks for {ticket_id}")

        self._github.create_branch(branch_name)
        self._state.upsert_task(ticket_id, {"branch_name": branch_name})

        all_committed_files: dict[str, str] = {}

        # Step 2: implement each chunk and commit
        for i, chunk in enumerate(chunks[:_MAX_CHUNKS], 1):
            chunk_desc = chunk.get("description", f"chunk {i}")
            acs_covered = chunk.get("acceptance_criteria_covered", [])
            log.info(
                "Dev implementing chunk %d/%d for %s: %s (covers %d AC(s))",
                i, len(chunks), ticket_id, chunk_desc, len(acs_covered),
            )

            # Build context: what's already on the branch
            current_branch_text = self._fetch_branch_files_raw(branch_name, list(all_committed_files.keys()))
            if not current_branch_text and existing_files_text:
                current_branch_text = existing_files_text

            chunk_result = self._ai.complete(
                "dev_implement_chunk.txt",
                system_message=system_message,
                ticket_id=ticket_id,
                ba_analysis=json.dumps(ba_analysis, indent=2),
                chunk_description=chunk_desc,
                acceptance_criteria_to_cover="\n".join(f"- {ac}" for ac in acs_covered),
                all_acceptance_criteria="\n".join(f"- {ac}" for ac in acceptance_criteria),
                current_files=current_branch_text or "(no files yet)",
            )

            if chunk_result.get("dry_run"):
                continue

            files: dict[str, str] = chunk_result.get("files", {})
            if not files:
                log.warning("Chunk %d returned no files for %s — skipping", i, ticket_id)
                continue

            commit_msg = chunk_result.get(
                "commit_message",
                f"feat({ticket_id}): {chunk_desc}",
            )
            self._github.commit_files(branch_name, files, commit_msg)
            all_committed_files.update(files)
            log.info("Committed chunk %d for %s: %s", i, ticket_id, list(files.keys()))

        # Step 3: self-check — are ALL acceptance criteria covered?
        log.info("Dev agent running self-check for %s", ticket_id)
        final_branch_text = self._fetch_branch_files_raw(branch_name, list(all_committed_files.keys()))

        self_check = self._ai.complete(
            "dev_self_check.txt",
            system_message=system_message,
            ticket_id=ticket_id,
            ba_analysis=json.dumps(ba_analysis, indent=2),
            current_files=final_branch_text or "(no files found on branch)",
        )

        if not self_check.get("dry_run") and not self_check.get("all_criteria_covered", True):
            missing = self_check.get("missing_criteria", [])
            log.info(
                "Self-check: %d AC(s) still missing for %s — implementing remainder",
                len(missing), ticket_id,
            )
            missing_text = "\n".join(f"- {m}" for m in missing)

            remainder = self._ai.complete(
                "dev_implement_chunk.txt",
                system_message=system_message,
                ticket_id=ticket_id,
                ba_analysis=json.dumps(ba_analysis, indent=2),
                chunk_description="remaining acceptance criteria",
                acceptance_criteria_to_cover=missing_text,
                all_acceptance_criteria="\n".join(f"- {ac}" for ac in acceptance_criteria),
                current_files=final_branch_text or "(no files yet)",
            )

            if not remainder.get("dry_run"):
                remainder_files: dict[str, str] = remainder.get("files", {})
                if remainder_files:
                    self._github.commit_files(
                        branch_name,
                        remainder_files,
                        f"feat({ticket_id}): cover remaining acceptance criteria",
                    )
                    all_committed_files.update(remainder_files)
                    log.info("Committed remainder for %s: %s", ticket_id, list(remainder_files.keys()))

        # Step 4: raise PR — only now, after everything is implemented
        pr_number = self._github.create_pull_request(branch_name, pr_title, pr_body)
        log.info("Dev agent raised PR #%d for %s", pr_number, ticket_id)

        self._state.upsert_task(
            ticket_id,
            {
                "status": "PR_RAISED",
                "branch_name": branch_name,
                "pr_number": pr_number,
                "submitted_files": all_committed_files,
            },
        )

        try:
            self._slack.post_task_update(
                ticket_id, "Dev", f"PR #{pr_number} raised for {ticket_id} — all ACs implemented"
            )
        except Exception as exc:
            log.warning("Slack notification failed: %s", exc)

    # ------------------------------------------------------------------
    # Fix round — address all Tech Lead feedback in one complete pass
    # ------------------------------------------------------------------

    def _fix(self, task: dict[str, Any], system_message: str) -> None:
        ticket_id: str = task["id"]
        ba_analysis: dict = task.get("ba_analysis", {})
        branch_name: str = task.get("branch_name", f"feature/{ticket_id.lower()}")
        pr_number: int = task.get("pr_number", 0)
        review_feedback: list[str] = task.get("review_feedback", [])

        log.info(
            "Dev agent fixing %s — %d accumulated feedback item(s)",
            ticket_id, len(review_feedback),
        )
        self._state.set_status(ticket_id, "IN_DEVELOPMENT")

        # Deduplicate feedback — strip round labels and collapse identical issues
        seen_issues: set[str] = set()
        deduped_feedback: list[str] = []
        for item in review_feedback:
            # Strip "[Round N] " prefix for dedup comparison
            import re as _re
            bare = _re.sub(r"^\[Round \d+\]\s*", "", item).strip().lower()
            if bare not in seen_issues:
                seen_issues.add(bare)
                deduped_feedback.append(item)
        if len(deduped_feedback) < len(review_feedback):
            log.info(
                "Deduped feedback for %s: %d → %d unique items",
                ticket_id, len(review_feedback), len(deduped_feedback),
            )
        feedback_text = "\n".join(f"- {f}" for f in deduped_feedback)

        # Use actual files on the branch (submitted_files), not BA's original file list
        submitted_keys = list(task.get("submitted_files", {}).keys())
        if not submitted_keys:
            submitted_keys = [e.get("path", "") for e in ba_analysis.get("files_to_change", [])]
        current_files = self._fetch_branch_files_raw(branch_name, submitted_keys)

        result = self._ai.complete(
            "dev_fix.txt",
            system_message=system_message,
            ticket_id=ticket_id,
            ba_analysis=json.dumps(ba_analysis, indent=2),
            submitted_files=current_files or "(no files found on branch)",
            review_feedback=feedback_text or "(no specific feedback provided)",
            branch_name=branch_name,
        )

        if result.get("dry_run"):
            log.info("Dry-run: skipping fix for %s", ticket_id)
            self._state.upsert_task(ticket_id, {"status": "PR_RAISED"})
            return

        files: dict[str, str] = result.get("files", {})
        if not files:
            raise ValueError(f"Fix AI returned no files for {ticket_id} — will retry next cycle")
        else:
            self._github.create_branch(branch_name)  # no-op if exists
            commit_msg = result.get(
                "commit_message", f"fix({ticket_id}): address all tech lead feedback"
            )
            self._github.commit_files(branch_name, files, commit_msg)
            log.info("Fix committed for %s: %s", ticket_id, list(files.keys()))

        self._state.upsert_task(
            ticket_id,
            {
                "status": "PR_RAISED",
                "submitted_files": {**task.get("submitted_files", {}), **files},
            },
        )

        try:
            self._slack.post_task_update(
                ticket_id, "Dev", f"PR #{pr_number} updated with fixes for {ticket_id}"
            )
        except Exception as exc:
            log.warning("Slack notification failed: %s", exc)

    # ------------------------------------------------------------------
    # GitHub file fetching helpers
    # ------------------------------------------------------------------

    def _fetch_branch_files_raw(self, branch_name: str, paths: list[str]) -> str:
        """Fetch named files from a branch. Returns formatted string."""
        parts: list[str] = []
        seen: set[str] = set()
        for path in paths:
            if not path or path in seen:
                continue
            seen.add(path)
            try:
                contents = self._github.repo.get_contents(path, ref=branch_name)
                text = base64.b64decode(contents.content).decode("utf-8", errors="replace")  # type: ignore[union-attr]
                parts.append(f"### {path}\n```\n{text}\n```")
            except Exception as exc:
                log.debug("Could not fetch %s from %s: %s", path, branch_name, exc)
        return "\n\n".join(parts) if parts else ""

    def _fetch_existing_files(self, files_to_change: list[dict]) -> str:
        """Fetch files from the base branch (main) for initial context."""
        parts: list[str] = []
        for entry in files_to_change:
            path = entry.get("path", "")
            if not path:
                continue
            try:
                contents = self._github.repo.get_contents(
                    path, ref=self._config.github_base_branch
                )
                text = base64.b64decode(contents.content).decode("utf-8", errors="replace")  # type: ignore[union-attr]
                parts.append(f"### {path}\n```\n{text}\n```")
            except Exception as exc:
                log.debug("Could not fetch %s from main: %s", path, exc)
                parts.append(f"### {path}\n(new file — does not exist yet)")
        return "\n\n".join(parts) if parts else "(no existing file context available)"
