"""
ai_client.py - Thin wrapper around the OpenAI chat completions API.

Responsibilities:
  - Load prompt templates from the prompts/ directory
  - Substitute {placeholders} using str.format_map (safe: unknown keys are left alone)
  - Call the OpenAI API with retry on transient errors
  - Parse and return the JSON payload that agents embed in their responses
  - Support a dry-run mode that logs the prompt without calling the API
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

import openai

from config import Config
from logger import get_logger

log = get_logger(__name__)

_PROMPT_DIR = Path(__file__).parent / "prompts"
_MAX_RETRIES = 3
_RETRY_DELAY = 5  # seconds
_MAX_TOKENS = 32000
_MAX_CONTINUATIONS = 5


class _SafeDict(dict):
    """dict subclass that returns empty string for missing keys in str.format_map."""
    def __missing__(self, key: str) -> str:
        log.warning("Prompt placeholder {%s} has no value — using empty string", key)
        return ""


class AIClient:
    def __init__(self, config: Config, dry_run: bool = False) -> None:
        self._client = openai.OpenAI(
            api_key=config.openai_api_key,
            base_url=config.openai_base_url,
        )
        self._model = config.openai_model
        self._dry_run = dry_run

    def load_prompt(self, prompt_file: str, **kwargs: str) -> str:
        path = _PROMPT_DIR / prompt_file
        if not path.exists():
            raise FileNotFoundError(f"Prompt file not found: {path}")
        template = path.read_text(encoding="utf-8")
        try:
            return template.format_map(kwargs)
        except KeyError as exc:
            log.warning("Prompt template key error — missing kwarg %s, substituting empty string", exc)
            return template.format_map(_SafeDict(kwargs))

    def complete(
        self,
        prompt_file: str,
        system_message: str = "You are a helpful software engineering assistant.",
        **prompt_kwargs: str,
    ) -> dict[str, Any]:
        # system_message may be passed as a kwarg by agents (injected knowledge)
        if "system_message" in prompt_kwargs:
            system_message = prompt_kwargs.pop("system_message")

        prompt = self.load_prompt(prompt_file, **prompt_kwargs)

        if self._dry_run:
            log.info(
                "[DRY-RUN] Would call model=%s prompt_file=%s kwargs=%s",
                self._model,
                prompt_file,
                list(prompt_kwargs.keys()),
            )
            log.debug("[DRY-RUN] Rendered prompt:\n%s", prompt)
            return {"dry_run": True, "prompt_file": prompt_file}

        messages: list[dict[str, str]] = [
            {"role": "system", "content": system_message},
            {"role": "user", "content": prompt},
        ]

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                content = self._call_with_continuation(messages)
                log.debug("Model raw response (first 500 chars): %s", content[:500])
                return self._extract_json(content)
            except openai.RateLimitError:
                wait = _RETRY_DELAY * attempt
                log.warning("Rate limit hit, waiting %ss (attempt %d)", wait, attempt)
                time.sleep(wait)
            except openai.APIConnectionError as exc:
                log.error("Connection error: %s", exc)
                if attempt == _MAX_RETRIES:
                    raise
                time.sleep(_RETRY_DELAY)
            except openai.APIStatusError as exc:
                log.error("API error %s: %s", exc.status_code, exc.message)
                raise

        raise RuntimeError(f"Model call failed after {_MAX_RETRIES} attempts")

    def _call_with_continuation(self, messages: list[dict[str, str]]) -> str:
        """
        Call the model and, if the response is cut off mid-output, ask it to
        continue from where it left off until the JSON is complete or we hit
        the continuation limit. This handles large file outputs that exceed
        the single-response token limit.
        """
        accumulated = ""

        for continuation in range(_MAX_CONTINUATIONS + 1):
            response = self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                temperature=0.2,
                max_tokens=_MAX_TOKENS,
            )
            choice = response.choices[0]
            chunk = choice.message.content or ""
            accumulated += chunk

            finish_reason = choice.finish_reason
            log.debug(
                "API call %d/%d finish_reason=%s accumulated_len=%d",
                continuation + 1,
                _MAX_CONTINUATIONS + 1,
                finish_reason,
                len(accumulated),
            )

            # "stop" means the model finished naturally — we're done
            if finish_reason == "stop":
                break

            # "length" means the output was cut off — ask the model to continue
            if finish_reason == "length":
                if continuation >= _MAX_CONTINUATIONS:
                    log.warning(
                        "Response still truncated after %d continuations — attempting partial parse",
                        _MAX_CONTINUATIONS,
                    )
                    break
                log.info(
                    "Response truncated (finish_reason=length) — requesting continuation %d",
                    continuation + 1,
                )
                # Append what we have so far as assistant turn, then ask to continue
                messages = messages + [
                    {"role": "assistant", "content": accumulated},
                    {"role": "user", "content": "Continue exactly from where you left off. Do not repeat anything already written."},
                ]
            else:
                # Unknown finish reason — stop and try to parse what we have
                log.warning("Unexpected finish_reason=%s — attempting parse", finish_reason)
                break

        return accumulated

    @staticmethod
    def _extract_json(text: str) -> dict[str, Any]:
        """
        Extract JSON from model output. Handles:
        - ```json ... ``` fences
        - Raw JSON
        - Truncated JSON (attempts repair by closing open structures)
        """
        # Strip markdown fence if present
        fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
        candidate = fence_match.group(1).strip() if fence_match else text.strip()

        # If no fence, find the first { ... } block
        if not candidate.startswith("{"):
            brace_match = re.search(r"\{[\s\S]*\}", candidate)
            if brace_match:
                candidate = brace_match.group(0)

        # Try parsing as-is first
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

        # Response may be truncated — attempt to close open JSON structures
        repaired = AIClient._repair_truncated_json(candidate)
        if repaired:
            try:
                result = json.loads(repaired)
                log.warning("Parsed truncated JSON after repair — some fields may be incomplete")
                return result
            except json.JSONDecodeError:
                pass

        raise ValueError(
            f"Could not parse JSON from model response\nRaw text (first 300 chars): {text[:300]}"
        )

    @staticmethod
    def _repair_truncated_json(text: str) -> str:
        """
        Attempt to close an incomplete JSON object by counting open braces,
        brackets, and quotes. Returns a repaired string or empty string if
        repair is not possible.
        """
        if not text.strip().startswith("{"):
            return ""

        # Truncate at the last complete key-value pair we can find
        # Strategy: find the last comma at the top brace level and cut there,
        # then close all open structures.
        depth = 0
        in_string = False
        escape_next = False
        last_safe_pos = 0

        for i, ch in enumerate(text):
            if escape_next:
                escape_next = False
                continue
            if ch == "\\" and in_string:
                escape_next = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch in ("{", "["):
                depth += 1
            elif ch in ("}", "]"):
                depth -= 1
                if depth == 0:
                    last_safe_pos = i
            elif ch == "," and depth == 1:
                last_safe_pos = i

        if last_safe_pos == 0:
            return ""

        # Cut at the last safe position and close the object
        truncated = text[: last_safe_pos].rstrip().rstrip(",")
        return truncated + "\n}"
