"""
github_client.py - Wrapper around the GitHub REST API (via PyGithub).

Handles all repository operations needed by the Developer and Tech Lead agents:
  - Branch creation from the base branch
  - File creation / update via the Git Data API
  - Pull request creation
  - CI status polling
  - PR merge (squash)
"""
from __future__ import annotations

import base64
import time
from typing import Any

import requests
import github
from github import Github, GithubException
from github.Repository import Repository

from config import Config
from logger import get_logger

log = get_logger(__name__)

_CI_POLL_INTERVAL = 30  # seconds between CI status checks
_CI_MAX_WAIT = 600  # bail out after 10 minutes


class GitHubClient:
    def __init__(self, config: Config, dry_run: bool = False) -> None:
        self._gh = Github(config.github_token)
        self._repo_name = config.github_repo
        self._base_branch = config.github_base_branch
        self._dry_run = dry_run
        self._token = config.github_token
        # Lazy-loaded to avoid auth check at construction time
        self._repo: Repository | None = None

    @property
    def repo(self) -> Repository:
        if self._repo is None:
            self._repo = self._gh.get_repo(self._repo_name)
        return self._repo

    def create_branch(self, branch_name: str) -> None:
        if self._dry_run:
            log.info("[DRY-RUN] Would create branch %s from %s", branch_name, self._base_branch)
            return
        try:
            base_sha = self.repo.get_branch(self._base_branch).commit.sha
            self.repo.create_git_ref(ref=f"refs/heads/{branch_name}", sha=base_sha)
            log.info("Created branch %s", branch_name)
        except GithubException as exc:
            if exc.status == 422:
                log.info("Branch %s already exists, reusing", branch_name)
            else:
                log.error("Failed to create branch %s: %s", branch_name, exc)
                raise

    def commit_files(self, branch_name: str, files: dict[str, str], commit_message: str) -> None:
        """
        Commit all files in a single atomic commit using the Git Trees API.
        files: {file_path: file_content}
        """
        if self._dry_run:
            log.info(
                "[DRY-RUN] Would commit %d file(s) to %s: %s",
                len(files),
                branch_name,
                list(files.keys()),
            )
            return

        # Get current HEAD commit and its tree
        ref = self.repo.get_git_ref(f"heads/{branch_name}")
        head_sha = ref.object.sha
        base_tree = self.repo.get_git_commit(head_sha).tree

        # Build tree elements — one blob per file
        tree_elements = []
        for path, content in files.items():
            blob = self.repo.create_git_blob(content, "utf-8")
            tree_elements.append(
                github.InputGitTreeElement(
                    path=path,
                    mode="100644",
                    type="blob",
                    sha=blob.sha,
                )
            )

        new_tree = self.repo.create_git_tree(tree_elements, base_tree)
        new_commit = self.repo.create_git_commit(
            message=commit_message,
            tree=new_tree,
            parents=[self.repo.get_git_commit(head_sha)],
        )
        ref.edit(new_commit.sha)
        log.info("Committed %d file(s) to %s: %s", len(files), branch_name, list(files.keys()))

    def create_pull_request(self, branch_name: str, title: str, body: str) -> int:
        """Create a PR and return its number."""
        if self._dry_run:
            log.info(
                "[DRY-RUN] Would create PR: %s (%s → %s)",
                title,
                branch_name,
                self._base_branch,
            )
            return 0
        try:
            pr = self.repo.create_pull(
                title=title,
                body=body,
                head=branch_name,
                base=self._base_branch,
            )
            log.info("Created PR #%d: %s", pr.number, title)
            return pr.number
        except GithubException as exc:
            if exc.status == 422:
                # PR already exists for this branch
                pulls = self.repo.get_pulls(
                    state="open",
                    head=f"{self._repo_name.split('/')[0]}:{branch_name}",
                )
                for pr in pulls:
                    log.info("PR already exists: #%d", pr.number)
                    return pr.number
            log.error("Failed to create PR: %s", exc)
            raise

    def get_ci_status(self, branch_name: str) -> str:
        """Return 'success', 'failure', or 'pending'."""
        try:
            branch = self.repo.get_branch(branch_name)
            commit = self.repo.get_commit(branch.commit.sha)
            statuses = list(commit.get_statuses())
            check_runs = list(commit.get_check_runs())

            # Evaluate check runs (Actions) first
            if check_runs:
                conclusions = [cr.conclusion for cr in check_runs if cr.status == "completed"]
                if any(c in ("failure", "cancelled", "timed_out") for c in conclusions):
                    return "failure"
                if len(conclusions) == len(check_runs) and all(c == "success" for c in conclusions):
                    return "success"
                return "pending"

            # Fall back to commit statuses
            if statuses:
                state_map = {"success": 0, "pending": 1, "failure": 2, "error": 3}
                worst = max(statuses, key=lambda s: state_map.get(s.state, 0))
                return worst.state

            # No checks and no statuses means the repo has no CI configured — treat as success
            return "success"
        except GithubException as exc:
            log.error("Failed to get CI status for %s: %s", branch_name, exc)
            return "pending"

    def wait_for_ci(self, branch_name: str) -> str:
        """Block until CI completes or timeout. Returns final status string."""
        log.info("Waiting for CI on branch %s (timeout %ds)...", branch_name, _CI_MAX_WAIT)
        elapsed = 0
        while elapsed < _CI_MAX_WAIT:
            status = self.get_ci_status(branch_name)
            if status in ("success", "failure"):
                log.info("CI finished on %s: %s", branch_name, status)
                return status
            log.info(
                "CI still pending on %s, checking again in %ds...",
                branch_name,
                _CI_POLL_INTERVAL,
            )
            time.sleep(_CI_POLL_INTERVAL)
            elapsed += _CI_POLL_INTERVAL
        log.warning("CI timed out for %s after %ds", branch_name, _CI_MAX_WAIT)
        return "timeout"

    def get_pr_diff(self, pr_number: int) -> str:
        """Fetch the unified diff for a pull request via the REST API."""
        url = f"https://api.github.com/repos/{self._repo_name}/pulls/{pr_number}"
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github.v3.diff",
        }
        try:
            resp = requests.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            return resp.text
        except requests.HTTPError as exc:
            log.error("Failed to fetch PR diff for #%d: %s", pr_number, exc)
            return ""

    def get_pr_diff_smart(self, pr_number: int, char_limit: int = 16000) -> str:
        """
        Return the PR diff, intelligently summarised if it exceeds char_limit.

        Strategy:
        - Parse the raw diff into per-file sections
        - If total fits within char_limit, return raw diff as-is
        - Otherwise build a structured summary:
            * Files with small diffs get their full diff included
            * Files with large diffs get a header + stats + truncated content
          Priority order: files with most changed lines first (most impactful to review)
        """
        raw = self.get_pr_diff(pr_number)
        if not raw or len(raw) <= char_limit:
            return raw

        # Parse into per-file sections
        file_sections: list[dict] = []
        current: dict | None = None
        for line in raw.splitlines(keepends=True):
            if line.startswith("diff --git "):
                if current:
                    file_sections.append(current)
                current = {"header": line, "lines": [], "path": ""}
            elif current is not None:
                if line.startswith("+++ b/") and not current["path"]:
                    current["path"] = line[6:].rstrip()
                current["lines"].append(line)
        if current:
            file_sections.append(current)

        if not file_sections:
            return raw[:char_limit] + "\n... [diff truncated]"

        # Compute stats per file
        for sec in file_sections:
            added = sum(1 for l in sec["lines"] if l.startswith("+") and not l.startswith("+++"))
            removed = sum(1 for l in sec["lines"] if l.startswith("-") and not l.startswith("---"))
            sec["added"] = added
            sec["removed"] = removed
            sec["content"] = sec["header"] + "".join(sec["lines"])
            sec["size"] = len(sec["content"])

        # Sort largest-change files first so reviewer sees the most important files
        file_sections.sort(key=lambda s: s["added"] + s["removed"], reverse=True)

        # Budget: try to fit as many full file diffs as possible
        _PER_FILE_BUDGET = 3000  # max chars per file before truncating
        parts: list[str] = []
        used = 0

        for sec in file_sections:
            remaining = char_limit - used - 200  # 200-char buffer for overhead
            if remaining <= 0:
                parts.append(f"diff --git a/{sec['path']} b/{sec['path']}\n"
                              f"# [{sec['added']} additions, {sec['removed']} deletions — omitted, budget exhausted]\n")
                continue

            if sec["size"] <= min(remaining, _PER_FILE_BUDGET):
                parts.append(sec["content"])
                used += sec["size"]
            else:
                # Include truncated diff with stats header
                allowed = min(remaining, _PER_FILE_BUDGET)
                truncated = sec["content"][:allowed]
                parts.append(
                    f"diff --git a/{sec['path']} b/{sec['path']}\n"
                    f"# [{sec['added']} additions, {sec['removed']} deletions — showing first {allowed} chars]\n"
                    + truncated
                    + f"\n# ... [{sec['path']} diff truncated]\n"
                )
                used += allowed + 100

        result = "\n".join(parts)
        log.info(
            "PR #%d diff: raw=%d chars → smart=%d chars across %d file(s)",
            pr_number, len(raw), len(result), len(file_sections),
        )
        return result

    def merge_pull_request(self, pr_number: int, commit_title: str) -> bool:
        """Squash-merge a PR. Returns True on success."""
        if self._dry_run:
            log.info("[DRY-RUN] Would merge PR #%d: %s", pr_number, commit_title)
            return True
        try:
            pr = self.repo.get_pull(pr_number)
            result = pr.merge(commit_title=commit_title, merge_method="squash")
            if result.merged:
                log.info("PR #%d merged successfully", pr_number)
                return True
            log.warning("PR #%d merge result: %s", pr_number, result.message)
            return False
        except GithubException as exc:
            log.error("Failed to merge PR #%d: %s", pr_number, exc)
            return False

    def get_ci_failure_logs(self, branch_name: str) -> str:
        """Fetch failed check-run annotations for dev agent to fix."""
        try:
            branch = self.repo.get_branch(branch_name)
            commit = self.repo.get_commit(branch.commit.sha)
            check_runs = list(commit.get_check_runs())
            failed = [
                cr
                for cr in check_runs
                if cr.conclusion in ("failure", "cancelled", "timed_out")
            ]
            if not failed:
                return "No failed check runs found."
            parts = []
            for cr in failed:
                annotations = list(cr.get_annotations())
                ann_text = (
                    "\n".join(
                        f"  {a.path}:{a.start_line} [{a.annotation_level}] {a.message}"
                        for a in annotations
                    )
                    or "  (no annotations)"
                )
                parts.append(f"Check: {cr.name}\n{ann_text}")
            return "\n\n".join(parts)
        except GithubException as exc:
            log.error("Failed to fetch CI logs: %s", exc)
            return "Could not retrieve CI failure logs."

    def approve_pull_request(self, pr_number: int, body: str = "LGTM — approved by Tech Lead agent.") -> bool:
        """Submit a GitHub review approval on the PR (does NOT merge)."""
        if self._dry_run:
            log.info("[DRY-RUN] Would approve PR #%d", pr_number)
            return True
        try:
            pr = self.repo.get_pull(pr_number)
            pr.create_review(body=body, event="APPROVE")
            log.info("PR #%d approved", pr_number)
            return True
        except GithubException as exc:
            log.error("Failed to approve PR #%d: %s", pr_number, exc)
            return False

    def post_review_comment(self, pr_number: int, body: str) -> bool:
        """
        Post review feedback on a PR as a plain issue comment.
        GitHub blocks REQUEST_CHANGES reviews when the reviewer is the PR author
        (same token), so we use a plain comment which always works.
        """
        if self._dry_run:
            log.info("[DRY-RUN] Would post review comment on PR #%d", pr_number)
            return True
        try:
            pr = self.repo.get_pull(pr_number)
            pr.create_issue_comment(body)
            log.info("Posted review comment on PR #%d", pr_number)
            return True
        except GithubException as exc:
            log.error("Failed to post review comment on PR #%d: %s", pr_number, exc)
            return False

    def test_connection(self) -> bool:
        """Return True if the token is valid and the repo is accessible."""
        try:
            _ = self.repo.full_name
            return True
        except GithubException as exc:
            log.error("GitHub auth failed: %s", exc)
            return False
