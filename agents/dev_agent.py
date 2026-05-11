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
import re
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

_TRIGGER_STATUSES = {
    "BA_DESCRIPTION_UPDATED",
    "CHANGES_REQUESTED",
    "BA_DONE",   # legacy
    "FIX_PENDING",  # legacy
}

# Maximum implementation chunks per ticket to avoid runaway loops
_MAX_CHUNKS = 8


def _fmt_commit(ticket_id: str, message: str) -> str:
    """Enforce commit message format: 'TICKET-ID | message'."""
    clean = re.sub(r"^[\w-]+\s*\|?\s*", "", message).strip() if "|" in message else message.strip()
    return f"{ticket_id} | {clean}"


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
                    ui.dev_skipped(task_id, f"status changed to {fresh_status}")
                    continue
                if fresh_status in ("CHANGES_REQUESTED", "FIX_PENDING"):
                    self._fix(fresh_task, system_message)
                else:
                    self._implement(fresh_task, system_message)
                processed.append(task_id)
            except Exception as exc:
                log.error("Dev agent failed on %s: %s", task_id, exc, exc_info=True)
                ui.task_failed(task_id, "Dev", exc)
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
        ui.dev_planning(ticket_id)
        self._state.set_status(ticket_id, "IN_DEVELOPMENT")

        branch_name: str = ticket_id  # always use ticket ID as branch name

        # Resolve target repos
        target_repo_keys: list[str] = ba_analysis.get("target_repos") or []
        if not target_repo_keys:
            raise ValueError(
                f"BA analysis for {ticket_id} has no target_repos — "
                "BA agent may have produced an incomplete analysis. Reset to BACKLOG to re-analyse."
            )
        github_clients: dict[str, Any] = {
            key: self._github.resolve_repo(key) for key in target_repo_keys
        }
        primary_gh = next(iter(github_clients.values()))

        # Resume from saved plan if available (restart recovery)
        saved_plan: dict = task.get("impl_plan", {})
        completed_chunks: int = task.get("completed_chunks", 0)
        all_committed_files: dict[str, str] = dict(task.get("submitted_files", {}))

        # Always fetch repo context — used in planning and every chunk implementation
        repo_context: dict[str, str] = self._fetch_repo_context(
            ba_analysis.get("files_to_change", []), github_clients
        )
        # Combined context across all repos for prompts that don't split by repo
        combined_context = "\n\n".join(
            f"## [{key} repo]\n{ctx}" for key, ctx in repo_context.items()
        )

        if saved_plan and completed_chunks > 0:
            log.info(
                "Resuming %s from chunk %d/%d (restart recovery)",
                ticket_id, completed_chunks + 1, len(saved_plan.get("chunks", [])),
            )
            chunks = saved_plan.get("chunks", [])
            pr_title = saved_plan.get("pr_title", f"feat: {ticket_id}")
            pr_body = saved_plan.get("pr_body", "")
        else:
            # Step 1: plan chunks (fresh start)
            plan = self._ai.complete(
                "dev_plan.txt",
                system_message=system_message,
                ticket_id=ticket_id,
                ba_analysis=json.dumps(ba_analysis, indent=2),
                existing_files=combined_context,
                ticket_id_lower=ticket_id.lower(),
            )

            if plan.get("dry_run"):
                log.info("Dry-run: skipping dev implementation for %s", ticket_id)
                self._state.upsert_task(ticket_id, {"status": "PR_RAISED"})
                return

            chunks = plan.get("chunks", [])
            pr_title = plan.get("pr_title", f"feat: {ticket_id}")
            pr_body = plan.get("pr_body", "")

            if not chunks:
                raise ValueError(f"AI returned no implementation chunks for {ticket_id}")

            # Save plan to state immediately so restarts can resume
            self._state.upsert_task(ticket_id, {
                "impl_plan": plan,
                "completed_chunks": 0,
                "branch_name": branch_name,
                "target_repos": target_repo_keys,
            })
            completed_chunks = 0

        # Build a path → repo_key lookup from the BA's files_to_change
        path_to_repo: dict[str, str] = {
            entry["path"]: entry["repo"]
            for entry in ba_analysis.get("files_to_change", [])
            if entry.get("path") and entry.get("repo")
        }

        # Create branches (no-op if already exist)
        for key, gh in github_clients.items():
            gh.create_branch(branch_name)
            log.info("Created branch %s in repo %s", branch_name, gh.repo.full_name)

        # Step 2: implement each chunk and commit, skipping already-completed ones
        for i, chunk in enumerate(chunks[:_MAX_CHUNKS], 1):
            if i <= completed_chunks:
                log.info("Skipping chunk %d/%d for %s — already committed", i, len(chunks), ticket_id)
                continue

            chunk_desc = chunk.get("description", f"chunk {i}")
            acs_covered = chunk.get("acceptance_criteria_covered", [])
            log.info(
                "Dev implementing chunk %d/%d for %s: %s (covers %d AC(s))",
                i, len(chunks), ticket_id, chunk_desc, len(acs_covered),
            )
            chunk_repo_key = chunk.get("repo_key", target_repo_keys[0])
            ui.dev_chunk(ticket_id, i, len(chunks), chunk_desc, chunk_repo_key)

            # Files already committed on this branch for this chunk's repo
            repo_committed_paths = [
                p for p in all_committed_files
                if path_to_repo.get(p, target_repo_keys[0]) == chunk_repo_key
            ]
            current_branch_text = self._fetch_branch_files_raw(branch_name, repo_committed_paths, github_clients.get(chunk_repo_key, primary_gh))

            # Repo context gives the AI the full picture: existing patterns + files to modify
            this_repo_context = repo_context.get(chunk_repo_key, combined_context)

            chunk_result = self._ai.complete(
                "dev_implement_chunk.txt",
                system_message=system_message,
                ticket_id=ticket_id,
                ba_analysis=json.dumps(ba_analysis, indent=2),
                chunk_description=chunk_desc,
                acceptance_criteria_to_cover="\n".join(f"- {ac}" for ac in acs_covered),
                all_acceptance_criteria="\n".join(f"- {ac}" for ac in acceptance_criteria),
                current_files=current_branch_text or "(no files committed yet for this repo)",
                repo_context=this_repo_context,
            )

            if chunk_result.get("dry_run"):
                continue

            files: dict[str, str] = chunk_result.get("files", {})
            if not files:
                log.warning("Chunk %d returned no files for %s — skipping", i, ticket_id)
                continue

            commit_msg = _fmt_commit(ticket_id, chunk_result.get("commit_message", chunk_desc))
            # Repo routing priority: plan chunk (set at plan time) → BA path map → primary
            repo_key = chunk.get("repo_key") or path_to_repo.get(next(iter(files), "")) or ""
            gh = github_clients.get(repo_key, primary_gh)
            gh.commit_files(branch_name, files, commit_msg)
            all_committed_files.update(files)

            # Save progress after every successful commit — restart resumes from here
            self._state.upsert_task(ticket_id, {
                "completed_chunks": i,
                "submitted_files": all_committed_files,
            })
            log.info("Committed chunk %d for %s to %s: %s", i, ticket_id, gh.repo.full_name, list(files.keys()))
            ui.dev_committed(ticket_id, i, len(chunks), repo_key, list(files.keys()))

        # Step 3: self-check — are ALL acceptance criteria covered?
        log.info("Dev agent running self-check for %s", ticket_id)
        ui.dev_self_check(ticket_id)
        # Fetch from every repo's branch for a complete picture
        all_branch_parts: list[str] = []
        for rkey, gh in github_clients.items():
            repo_paths = [p for p in all_committed_files if path_to_repo.get(p, target_repo_keys[0]) == rkey]
            text = self._fetch_branch_files_raw(branch_name, repo_paths, gh)
            if text:
                all_branch_parts.append(f"## [{rkey} repo]\n{text}")
        final_branch_text = "\n\n".join(all_branch_parts)

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
            ui.dev_self_check_gap(ticket_id, len(missing))
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
                repo_context=combined_context,
            )

            if not remainder.get("dry_run"):
                remainder_files: dict[str, str] = remainder.get("files", {})
                if remainder_files:
                    primary_gh.commit_files(
                        branch_name,
                        remainder_files,
                        _fmt_commit(ticket_id, "cover remaining acceptance criteria"),
                    )
                    all_committed_files.update(remainder_files)
                    log.info("Committed remainder for %s: %s", ticket_id, list(remainder_files.keys()))

        # Step 4: raise one PR per repo — only now, after everything is implemented
        pr_numbers: dict[str, int] = {}
        for key, gh in github_clients.items():
            try:
                pr_num = gh.create_pull_request(branch_name, pr_title, pr_body)
                pr_numbers[key] = pr_num
                log.info("Dev agent raised PR #%d in %s for %s", pr_num, gh.repo_name, ticket_id)
            except Exception as exc:
                # 422 = no commits on branch (repo had no changes for this ticket)
                log.warning("Skipping PR for repo %s (%s): %s", key, gh.repo_name, exc)

        # Primary PR number stored for Tech Lead to review
        primary_pr = pr_numbers.get(target_repo_keys[0], 0)

        self._state.upsert_task(
            ticket_id,
            {
                "status": "PR_RAISED",
                "branch_name": branch_name,
                "pr_number": primary_pr,
                "pr_numbers": pr_numbers,
                "target_repos": target_repo_keys,
                "submitted_files": all_committed_files,
                # Clear resume fields — no longer needed once PR is raised
                "impl_plan": None,
                "completed_chunks": 0,
            },
        )

        pr_summary = ", ".join(f"#{n} ({k})" for k, n in pr_numbers.items())
        ui.dev_pr_raised(ticket_id, pr_summary)
        try:
            self._slack.post_task_update(
                ticket_id, "Dev", f"PRs raised for {ticket_id}: {pr_summary} — all ACs implemented"
            )
        except Exception as exc:
            log.warning("Slack notification failed: %s", exc)

    # ------------------------------------------------------------------
    # Fix round — address all Tech Lead feedback in one complete pass
    # ------------------------------------------------------------------

    def _fix(self, task: dict[str, Any], system_message: str) -> None:
        ticket_id: str = task["id"]
        ba_analysis: dict = task.get("ba_analysis", {})
        branch_name: str = task.get("branch_name", ticket_id)
        pr_number: int = task.get("pr_number", 0)
        review_feedback: list[str] = task.get("review_feedback", [])

        log.info(
            "Dev agent fixing %s — %d accumulated feedback item(s)",
            ticket_id, len(review_feedback),
        )
        ui.dev_fixing(ticket_id, len(review_feedback))
        self._state.set_status(ticket_id, "IN_DEVELOPMENT")

        # Resolve repo clients (same as _implement)
        target_repo_keys: list[str] = ba_analysis.get("target_repos", ["default"])
        github_clients: dict[str, Any] = {
            key: self._github.resolve_repo(key) for key in target_repo_keys
        }
        primary_gh = next(iter(github_clients.values()))

        # Build path → repo_key lookup from BA plan
        path_to_repo: dict[str, str] = {
            entry["path"]: entry["repo"]
            for entry in ba_analysis.get("files_to_change", [])
            if entry.get("path") and entry.get("repo")
        }

        # Deduplicate feedback — strip round labels and collapse identical issues
        seen_issues: set[str] = set()
        deduped_feedback: list[str] = []
        for item in review_feedback:
            bare = re.sub(r"^\[Round \d+\]\s*", "", item).strip().lower()
            if bare not in seen_issues:
                seen_issues.add(bare)
                deduped_feedback.append(item)
        if len(deduped_feedback) < len(review_feedback):
            log.info(
                "Deduped feedback for %s: %d → %d unique items",
                ticket_id, len(review_feedback), len(deduped_feedback),
            )
        feedback_text = "\n".join(f"- {f}" for f in deduped_feedback)

        # Fetch repo context so fixes match existing codebase patterns
        fix_repo_context = self._fetch_repo_context(
            ba_analysis.get("files_to_change", []), github_clients
        )
        fix_combined_context = "\n\n".join(
            f"## [{key} repo]\n{ctx}" for key, ctx in fix_repo_context.items()
        )

        # Fetch actual files on the branch per repo
        all_branch_parts: list[str] = []
        for rkey, gh in github_clients.items():
            submitted_keys = [
                p for p in task.get("submitted_files", {})
                if path_to_repo.get(p, target_repo_keys[0]) == rkey
            ]
            if not submitted_keys:
                submitted_keys = [
                    e.get("path", "") for e in ba_analysis.get("files_to_change", [])
                    if e.get("repo") == rkey
                ]
            text = self._fetch_branch_files_raw(branch_name, submitted_keys, gh)
            if text:
                all_branch_parts.append(f"## [{rkey} repo]\n{text}")
        current_files = "\n\n".join(all_branch_parts)

        result = self._ai.complete(
            "dev_fix.txt",
            system_message=system_message,
            ticket_id=ticket_id,
            ba_analysis=json.dumps(ba_analysis, indent=2),
            submitted_files=current_files or "(no files found on branch)",
            review_feedback=feedback_text or "(no specific feedback provided)",
            branch_name=branch_name,
            repo_context=fix_combined_context,
        )

        if result.get("dry_run"):
            log.info("Dry-run: skipping fix for %s", ticket_id)
            self._state.upsert_task(ticket_id, {"status": "PR_RAISED"})
            return

        files: dict[str, str] = result.get("files", {})
        if not files:
            raise ValueError(f"Fix AI returned no files for {ticket_id} — will retry next cycle")

        # Group fixed files by repo and commit to the right client
        files_by_repo: dict[str, dict[str, str]] = {}
        for path, content in files.items():
            repo_key = path_to_repo.get(path, target_repo_keys[0])
            files_by_repo.setdefault(repo_key, {})[path] = content

        commit_msg = _fmt_commit(ticket_id, result.get("commit_message", "address all tech lead feedback"))
        for repo_key, repo_files in files_by_repo.items():
            gh = github_clients.get(repo_key, primary_gh)
            gh.create_branch(branch_name)  # no-op if exists
            gh.commit_files(branch_name, repo_files, commit_msg)
            log.info("Fix committed for %s to %s: %s", ticket_id, gh.repo.full_name, list(repo_files.keys()))
            ui.dev_fix_committed(ticket_id, repo_key)

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

    def _fetch_repo_context(self, files_to_change: list[dict], github_clients: dict) -> dict[str, str]:
        """
        For each repo, fetch:
        1. Current content of every planned file (from main — so AI sees what it's modifying)
        2. Related files in the same directories (so AI sees naming/style conventions)
        3. Key shared files: types, api client, hooks, app entry points

        Returns {repo_key: formatted_context_string}
        """
        # Key shared files to always fetch per repo — these define the patterns the AI must follow
        _SHARED_PATTERNS: dict[str, list[str]] = {
            "ui": [
                "src/types/api.ts",
                "src/api/client.ts",
                "src/api/endpoints.ts",
                "src/hooks/useApi.ts",
                "src/main.tsx",
                "src/routes/__root.tsx",
                "src/styles/_variables.scss",
                "src/styles/_mixins.scss",
            ],
            "api": [
                "src/main/java/com/revelio/api/config/SecurityConfig.java",
                "src/main/java/com/revelio/api/dto/ApiResponse.java",
                "src/main/java/com/revelio/api/controller/HealthController.java",
                "src/main/java/com/revelio/api/service/HealthService.java",
            ],
        }

        # Group planned files by repo
        files_by_repo: dict[str, list[str]] = {}
        for entry in files_to_change:
            repo_key = entry.get("repo", "")
            path = entry.get("path", "")
            if repo_key and path:
                files_by_repo.setdefault(repo_key, []).append(path)

        result: dict[str, str] = {}
        for repo_key, gh in github_clients.items():
            parts: list[str] = []
            fetched: set[str] = set()
            base = self._config.github_base_branch

            def _fetch_file(path: str, label: str = "") -> None:
                if path in fetched:
                    return
                fetched.add(path)
                try:
                    contents = gh.repo.get_contents(path, ref=base)
                    text = base64.b64decode(contents.content).decode("utf-8", errors="replace")  # type: ignore[union-attr]
                    header = f"### {path}" + (f" ({label})" if label else "")
                    parts.append(f"{header}\n```\n{text}\n```")
                except Exception:
                    if label == "planned":
                        parts.append(f"### {path}\n(new file — does not exist yet on main)")

            # 1. Shared pattern files for this repo
            for shared_path in _SHARED_PATTERNS.get(repo_key, []):
                _fetch_file(shared_path, "existing pattern — follow this style")

            # 2. Planned files (what already exists that the agent will modify)
            for path in files_by_repo.get(repo_key, []):
                _fetch_file(path, "planned")

            # 3. Sibling files in the same directories as planned files (for naming/style context)
            dirs_seen: set[str] = set()
            for path in files_by_repo.get(repo_key, []):
                dir_path = "/".join(path.split("/")[:-1])
                if not dir_path or dir_path in dirs_seen:
                    continue
                dirs_seen.add(dir_path)
                try:
                    siblings = gh.repo.get_contents(dir_path, ref=base)
                    for sibling in (siblings if isinstance(siblings, list) else [siblings]):
                        if sibling.type == "file" and sibling.path not in fetched:
                            _fetch_file(sibling.path, "sibling — follow this style")
                except Exception:
                    pass

            result[repo_key] = "\n\n".join(parts) if parts else "(no existing context — this is a new codebase)"
            log.info(
                "Repo context for %s: %d file(s) fetched (%d chars)",
                repo_key, len(fetched), len(result[repo_key]),
            )

        return result

    def _fetch_branch_files_raw(self, branch_name: str, paths: list[str], gh: GitHubClient | None = None) -> str:
        """Fetch named files from a branch. Returns formatted string."""
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
        return "\n\n".join(parts) if parts else ""

