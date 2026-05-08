"""
slack_client.py - Wrapper around the Slack Web API (via slack-sdk).
"""
from __future__ import annotations

from typing import Any

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from config import Config
from logger import get_logger

log = get_logger(__name__)


class SlackClient:
    def __init__(self, config: Config, dry_run: bool = False) -> None:
        self._client = WebClient(token=config.slack_bot_token)
        self._channel = config.slack_channel_id
        # Clarification questions go to a dedicated channel where the bot is a member.
        # Falls back to the main channel if not configured.
        self._clarification_channel = config.slack_clarification_channel_id or config.slack_channel_id
        self._dry_run = dry_run

    def post_message(self, text: str, blocks: list[dict] | None = None, thread_ts: str | None = None, use_clarification_channel: bool = False) -> str | None:
        """
        Post a message to the configured channel.
        Returns the message timestamp (ts) on success, None on failure.
        Pass thread_ts to reply inside a thread.
        Pass use_clarification_channel=True to post to the clarification channel instead.
        """
        channel = self._clarification_channel if use_clarification_channel else self._channel
        if self._dry_run:
            log.info("[DRY-RUN] Slack post channel=%s text=%s", channel, text[:100])
            return "dry-run-ts"
        try:
            kwargs: dict[str, Any] = {"channel": channel, "text": text}
            if blocks:
                kwargs["blocks"] = blocks
            if thread_ts:
                kwargs["thread_ts"] = thread_ts
            resp = self._client.chat_postMessage(**kwargs)
            ts = resp.get("ts")
            log.debug("Slack message sent ts=%s", ts)
            return ts
        except SlackApiError as exc:
            log.error("Slack API error: %s", exc.response["error"])
            return None

    def post_clarification_request(self, ticket_id: str, summary: str, questions: list[str]) -> str | None:
        """
        Post a clarification request as a rich Slack message.
        Returns the thread_ts so replies can be polled later.
        """
        questions_text = "\n".join(f"{i + 1}. {q}" for i, q in enumerate(questions))
        plain_text = (
            f"[BA Agent] Clarification needed for {ticket_id}: {summary}\n\n"
            f"{questions_text}\n\n"
            f"Please reply in this thread to unblock the ticket."
        )
        blocks = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"🤔 Clarification Needed: {ticket_id}"},
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Ticket:* {summary}"},
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Questions:*\n{questions_text}",
                },
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": "Reply in this thread to answer. The BA agent polls every cycle and will proceed once all questions are answered.",
                    }
                ],
            },
        ]
        if self._dry_run:
            log.info("[DRY-RUN] Would post clarification for %s to %s", ticket_id, self._clarification_channel)
            return "dry-run-ts"
        try:
            kwargs: dict[str, Any] = {
                "channel": self._clarification_channel,
                "text": plain_text,
                "blocks": blocks,
            }
            resp = self._client.chat_postMessage(**kwargs)
            ts = resp.get("ts")
            log.info("Clarification posted to %s ts=%s", self._clarification_channel, ts)
            return ts
        except SlackApiError as exc:
            log.error("Failed to post clarification: %s", exc.response["error"])
            return None

    def get_thread_replies(self, thread_ts: str) -> list[dict[str, Any]]:
        """
        Fetch all replies in a thread. Returns a list of message dicts.
        Excludes the original bot message (the question post itself).
        """
        if self._dry_run:
            log.info("[DRY-RUN] Would poll thread %s for replies", thread_ts)
            return []
        try:
            resp = self._client.conversations_replies(
                channel=self._clarification_channel,
                ts=thread_ts,
            )
            messages = resp.get("messages", [])
            # First message is the original question — skip it
            replies = messages[1:] if len(messages) > 1 else []
            log.debug("Thread %s has %d reply/replies", thread_ts, len(replies))
            return replies
        except SlackApiError as exc:
            error = exc.response["error"]
            if error == "not_in_channel":
                log.error(
                    "Bot is not a member of the channel — run '/invite @<bot-name>' in Slack to fix this"
                )
            else:
                log.error("Failed to fetch thread replies for %s: %s", thread_ts, error)
            return []

    def post_task_update(self, task_id: str, phase: str, summary: str) -> None:
        """Convenience wrapper for the standard agent-update message format."""
        text = f"[{phase}] {task_id}: {summary}"
        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*[{phase}]* `{task_id}`\n{summary}",
                },
            }
        ]
        self.post_message(text, blocks)

    def test_connection(self) -> bool:
        """Return True if the bot token is valid and the channel is accessible."""
        try:
            resp = self._client.auth_test()
            if not resp["ok"]:
                log.error("Slack auth_test failed: %s", resp.get("error"))
                return False
            self._client.conversations_info(channel=self._channel)
            return True
        except SlackApiError as exc:
            log.error("Slack connection test failed: %s", exc.response["error"])
            return False
