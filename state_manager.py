"""
state_manager.py - Persistent task state with distributed locking.

Multiple agent processes can run simultaneously. The locking protocol:
  - Before claiming a ticket, an agent calls claim_task(ticket_id, agent_name)
  - This atomically creates locks/<ticket_id>.lock using O_CREAT|O_EXCL
  - Only one process succeeds; others skip that ticket this cycle
  - The agent calls release_task(ticket_id) when done (success or error)
  - Stale locks (process died mid-work) are cleaned up after LOCK_TTL_SECONDS

State file writes use write-to-tmp-then-rename for crash safety.

Task lifecycle:
  NEW → BA_ANALYZING → BA_AWAITING_CLARIFICATION → BA_DESCRIPTION_UPDATED →
  IN_DEVELOPMENT → PR_RAISED → UNDER_REVIEW →
    ├→ CHANGES_REQUESTED → (back to IN_DEVELOPMENT) [loop — no limit]
    └→ APPROVED  (terminal — human merges manually)
  FAILED  (terminal — unrecoverable error)
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from logger import get_logger

log = get_logger(__name__)

LOCK_TTL_SECONDS = 600  # locks older than this are assumed stale

STATUSES = {
    "NEW",
    "BA_ANALYZING",
    "BA_AWAITING_CLARIFICATION",
    "BA_DESCRIPTION_UPDATED",
    "IN_DEVELOPMENT",
    "PR_RAISED",
    "UNDER_REVIEW",
    "CHANGES_REQUESTED",
    "APPROVED",
    "FAILED",
    # Legacy values
    "BA_PENDING", "BA_DONE", "DEV_PENDING", "DEV_DONE",
    "REVIEW_PENDING", "REVIEW_DONE", "FIX_PENDING", "MERGED",
}


class StateManager:
    def __init__(self, state_file: str = "state.json") -> None:
        self._path = Path(state_file)
        self._tmp_path = Path(f"{state_file}.tmp")
        self._lock_dir = self._path.parent / "locks"
        self._lock_dir.mkdir(exist_ok=True)

    # ------------------------------------------------------------------
    # Distributed locking
    # ------------------------------------------------------------------

    def claim_task(self, task_id: str, agent_name: str) -> bool:
        """
        Atomically claim a ticket for processing.
        Returns True if this process won the claim, False if another holds it.
        Cleans up stale locks automatically.
        """
        lock_path = self._lock_dir / f"{task_id}.lock"

        # Clean up stale lock first (process died without releasing)
        if lock_path.exists():
            age = time.time() - lock_path.stat().st_mtime
            if age > LOCK_TTL_SECONDS:
                log.warning("Removing stale lock for %s (age=%.0fs)", task_id, age)
                try:
                    lock_path.unlink(missing_ok=True)
                except OSError:
                    pass
            else:
                log.debug("Ticket %s is locked by another process (age=%.0fs)", task_id, age)
                return False

        # O_CREAT | O_EXCL is atomic — only one process succeeds
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"{agent_name}\n{os.getpid()}\n{time.time()}".encode())
            os.close(fd)
            log.debug("Claimed lock for %s (%s pid=%d)", task_id, agent_name, os.getpid())
            return True
        except FileExistsError:
            log.debug("Lost race for %s lock — another process claimed it", task_id)
            return False

    def release_task(self, task_id: str) -> None:
        """Release the lock for a ticket."""
        lock_path = self._lock_dir / f"{task_id}.lock"
        try:
            lock_path.unlink(missing_ok=True)
            log.debug("Released lock for %s", task_id)
        except OSError as exc:
            log.warning("Could not release lock for %s: %s", task_id, exc)

    # ------------------------------------------------------------------
    # State file (re-read on every access so all processes see latest)
    # ------------------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        if self._path.exists():
            try:
                with self._path.open() as fh:
                    return json.load(fh)
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("State file unreadable: %s", exc)
        return {"tasks": {}}

    def _save(self, state: dict[str, Any]) -> None:
        self._tmp_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        os.replace(self._tmp_path, self._path)

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        return self._load()["tasks"].get(task_id)

    def upsert_task(self, task_id: str, updates: dict[str, Any]) -> None:
        """Read-modify-write with fresh load each time for multi-process safety."""
        state = self._load()
        task = state["tasks"].setdefault(task_id, {"id": task_id})
        task.update(updates)
        self._save(state)
        log.debug("State updated for %s: %s", task_id, list(updates.keys()))

    def set_status(self, task_id: str, status: str) -> None:
        if status not in STATUSES:
            raise ValueError(f"Unknown status '{status}'. Valid: {STATUSES}")
        self.upsert_task(task_id, {"status": status})
        log.info("Task %s → %s", task_id, status)

    def get_status(self, task_id: str) -> str | None:
        task = self.get_task(task_id)
        return task.get("status") if task else None

    def all_tasks(self) -> dict[str, Any]:
        return dict(self._load().get("tasks", {}))

    def recover_stuck_tasks(self) -> None:
        """
        On agent startup, reset any tickets stuck in transient in-progress statuses
        back to the last actionable status so they get picked up again.

        Safe to call from multiple agents simultaneously — each uses a different
        recovery mapping so they won't conflict.

        Recovery map:
          IN_DEVELOPMENT  → BA_DESCRIPTION_UPDATED  (Dev picks it up)
          UNDER_REVIEW    → PR_RAISED               (Tech Lead picks it up)
          BA_ANALYZING    → (dropped from state, BA re-fetches from Jira)
        """
        _recovery = {
            "IN_DEVELOPMENT": "BA_DESCRIPTION_UPDATED",
            "UNDER_REVIEW": "PR_RAISED",
        }
        state = self._load()
        changed = False
        for task_id, task in state.get("tasks", {}).items():
            status = task.get("status")
            # Only recover if no active lock — a live agent may genuinely hold it
            lock_path = self._lock_dir / f"{task_id}.lock"
            if lock_path.exists():
                age = time.time() - lock_path.stat().st_mtime
                if age <= LOCK_TTL_SECONDS:
                    continue  # another process is actively working on it
                lock_path.unlink(missing_ok=True)  # stale — clean it up

            if status in _recovery:
                new_status = _recovery[status]
                log.warning(
                    "Recovering stuck task %s: %s → %s", task_id, status, new_status
                )
                task["status"] = new_status
                changed = True
            elif status == "BA_ANALYZING":
                # BA was mid-analysis — safest to let BA re-fetch from Jira
                # Remove from state so BA treats it as a new ticket
                log.warning("Removing stuck BA_ANALYZING task %s — BA will re-fetch from Jira", task_id)
                del state["tasks"][task_id]
                changed = True

        if changed:
            self._save(state)

    def increment_fix_attempts(self, task_id: str) -> int:
        state = self._load()
        task = state["tasks"].setdefault(task_id, {"id": task_id})
        attempts = task.get("fix_attempts", 0) + 1
        task["fix_attempts"] = attempts
        self._save(state)
        return attempts
