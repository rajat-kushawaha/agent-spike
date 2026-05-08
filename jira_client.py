"""
jira_client.py - Wrapper around the Jira REST API.

Operations exposed:
  - Fetch issues by status (Backlog, In Progress, etc.)
  - Transition a ticket to a new status
  - Add a comment
  - Update the description field directly (ADF format)
  - Update arbitrary fields
"""
from __future__ import annotations

from typing import Any

import requests
from requests.auth import HTTPBasicAuth

from config import Config
from logger import get_logger

log = get_logger(__name__)


class JiraClient:
    def __init__(self, config: Config, dry_run: bool = False) -> None:
        self._base = config.jira_base_url.rstrip("/")
        self._auth = HTTPBasicAuth(config.jira_email, config.jira_api_token)
        self._project = config.jira_project_key
        self._dry_run = dry_run
        self._headers = {"Content-Type": "application/json", "Accept": "application/json"}
        # Status names read from config so the board's exact names are used
        self._status_backlog = config.jira_status_backlog
        self._status_in_progress = config.jira_status_in_progress

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _get(self, path: str, params: dict | None = None) -> Any:
        url = f"{self._base}{path}"
        try:
            resp = requests.get(
                url, auth=self._auth, headers=self._headers, params=params, timeout=30
            )
            resp.raise_for_status()
            return resp.json()
        except requests.HTTPError as exc:
            log.error(
                "Jira GET %s failed: %s %s",
                path,
                exc.response.status_code,
                exc.response.text[:200],
            )
            raise
        except requests.RequestException as exc:
            log.error("Jira GET %s request error: %s", path, exc)
            raise

    def _post(self, path: str, body: dict) -> Any:
        url = f"{self._base}{path}"
        if self._dry_run:
            log.info("[DRY-RUN] Jira POST %s body=%s", path, str(body)[:200])
            return {}
        try:
            resp = requests.post(
                url, auth=self._auth, headers=self._headers, json=body, timeout=30
            )
            resp.raise_for_status()
            return resp.json() if resp.content else {}
        except requests.HTTPError as exc:
            log.error(
                "Jira POST %s failed: %s %s",
                path,
                exc.response.status_code,
                exc.response.text[:200],
            )
            raise
        except requests.RequestException as exc:
            log.error("Jira POST %s request error: %s", path, exc)
            raise

    def _put(self, path: str, body: dict) -> None:
        url = f"{self._base}{path}"
        if self._dry_run:
            log.info("[DRY-RUN] Jira PUT %s body=%s", path, str(body)[:200])
            return
        try:
            resp = requests.put(
                url, auth=self._auth, headers=self._headers, json=body, timeout=30
            )
            resp.raise_for_status()
        except requests.HTTPError as exc:
            log.error(
                "Jira PUT %s failed: %s %s",
                path,
                exc.response.status_code,
                exc.response.text[:200],
            )
            raise
        except requests.RequestException as exc:
            log.error("Jira PUT %s request error: %s", path, exc)
            raise

    # ------------------------------------------------------------------
    # Issue queries
    # ------------------------------------------------------------------

    def _search(self, status: str) -> list[dict[str, Any]]:
        jql = (
            f'project = "{self._project}" AND status = "{status}" ORDER BY created ASC'
        )
        data = self._get(
            "/rest/api/3/search/jql",
            params={
                "jql": jql,
                "maxResults": 50,
                "fields": "summary,description,status,assignee,comment,labels",
            },
        )
        return data.get("issues", [])

    def get_backlog_issues(self) -> list[dict[str, Any]]:
        """Fetch all issues in the configured Backlog status."""
        return self._search(self._status_backlog)

    def get_in_progress_issues(self) -> list[dict[str, Any]]:
        """Fetch all issues in the configured In Progress status."""
        return self._search(self._status_in_progress)

    def get_issue(self, issue_key: str) -> dict[str, Any]:
        return self._get(f"/rest/api/3/issue/{issue_key}")

    # ------------------------------------------------------------------
    # Issue mutations
    # ------------------------------------------------------------------

    def add_comment(self, issue_key: str, body: str) -> None:
        log.info("Adding Jira comment to %s", issue_key)
        self._post(
            f"/rest/api/3/issue/{issue_key}/comment",
            {
                "body": {
                    "type": "doc",
                    "version": 1,
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [{"type": "text", "text": body}],
                        }
                    ],
                }
            },
        )

    def append_to_description(self, issue_key: str, appended_text: str) -> None:
        """
        Append text below the existing description rather than replacing it.
        Fetches current description first, then writes original + divider + new content.
        """
        log.info("Appending to description for %s", issue_key)
        try:
            issue = self._get(f"/rest/api/3/issue/{issue_key}", params={"fields": "description"})
            existing_adf = issue.get("fields", {}).get("description")
        except Exception as exc:
            log.warning("Could not fetch existing description for %s: %s — will append anyway", issue_key, exc)
            existing_adf = None

        # Build new ADF nodes: keep original content, add a divider, then append
        original_content: list[dict] = []
        if existing_adf and isinstance(existing_adf, dict):
            original_content = existing_adf.get("content", [])

        divider: dict = {"type": "rule"}  # ADF horizontal rule
        appended_nodes: list[dict] = [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": line if line else " "}],
            }
            for line in appended_text.splitlines()
        ]

        new_content = original_content + ([divider] if original_content else []) + appended_nodes

        self._put(
            f"/rest/api/3/issue/{issue_key}",
            {
                "fields": {
                    "description": {
                        "type": "doc",
                        "version": 1,
                        "content": new_content,
                    }
                }
            },
        )

    def transition_issue(self, issue_key: str, transition_name: str) -> None:
        """Transition a Jira issue by transition name (case-insensitive match)."""
        transitions = self._get(f"/rest/api/3/issue/{issue_key}/transitions")
        match = next(
            (
                t
                for t in transitions.get("transitions", [])
                if t["name"].lower() == transition_name.lower()
            ),
            None,
        )
        if not match:
            available = [t["name"] for t in transitions.get("transitions", [])]
            log.warning(
                "Transition '%s' not found for %s. Available: %s",
                transition_name,
                issue_key,
                available,
            )
            return
        self._post(
            f"/rest/api/3/issue/{issue_key}/transitions",
            {"transition": {"id": match["id"]}},
        )
        log.info("Transitioned %s → %s", issue_key, transition_name)

    def update_issue(self, issue_key: str, fields: dict) -> None:
        self._put(f"/rest/api/3/issue/{issue_key}", {"fields": fields})

    def test_connection(self) -> bool:
        """Return True if credentials are valid."""
        try:
            self._get("/rest/api/3/myself")
            return True
        except requests.HTTPError as exc:
            log.error("Jira auth failed: %s", exc)
            return False
        except requests.RequestException as exc:
            log.error("Jira connection error: %s", exc)
            return False
