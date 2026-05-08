"""
validate_setup.py - Pre-flight checks for the agent-spike system.

Checks:
  1. All required .env variables are present
  2. OpenAI authentication works
  3. Jira authentication works
  4. Slack bot token is valid and channel is accessible
  5. GitHub token is valid and repo is accessible

Usage:
  python validate_setup.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

# ANSI colours for pass / fail output
GREEN = "\033[92m"
RED = "\033[91m"
RESET = "\033[0m"
BOLD = "\033[1m"

_REQUIRED_VARS = [
    "OPENAI_API_KEY",
    "JIRA_BASE_URL",
    "JIRA_EMAIL",
    "JIRA_API_TOKEN",
    "SLACK_BOT_TOKEN",
    "SLACK_CHANNEL_ID",
    "GITHUB_TOKEN",
    "GITHUB_REPO",
]

_pass_count = 0
_fail_count = 0


def check(label: str, passed: bool, detail: str = "") -> None:
    global _pass_count, _fail_count
    icon = f"{GREEN}[PASS]{RESET}" if passed else f"{RED}[FAIL]{RESET}"
    msg = f"{icon} {label}"
    if detail:
        msg += f"\n   {detail}"
    print(msg)
    if passed:
        _pass_count += 1
    else:
        _fail_count += 1


def check_env_vars() -> bool:
    print(f"\n{BOLD}=== Environment Variables ==={RESET}")
    all_present = True
    for var in _REQUIRED_VARS:
        val = os.getenv(var, "")
        present = bool(val) and "REPLACE" not in val and "your_" not in val.lower()
        check(
            f"{var}",
            present,
            "" if present else "Not set or still a placeholder — edit .env",
        )
        if not present:
            all_present = False
    return all_present


def check_openai() -> None:
    # Validates against OpenRouter if OPENAI_BASE_URL is set, otherwise OpenAI directly
    base_url = os.getenv("OPENAI_BASE_URL", "")
    is_openrouter = "openrouter" in base_url
    label = "OpenRouter" if is_openrouter else "OpenAI"
    print(f"\n{BOLD}=== {label} ==={RESET}")
    try:
        import requests as _req
        api_key = os.getenv("OPENAI_API_KEY", "")
        if is_openrouter:
            resp = _req.get(
                "https://openrouter.ai/api/v1/auth/key",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json().get("data", {})
                remaining = data.get("limit_remaining", "?")
                check(f"{label} authentication", True, f"Key valid — {remaining} credits remaining")
            elif resp.status_code == 401:
                check(f"{label} authentication", False, "Invalid key — check OPENAI_API_KEY in .env")
            else:
                check(f"{label} authentication", False, f"HTTP {resp.status_code}: {resp.text[:100]}")
        else:
            import openai
            client = openai.OpenAI(api_key=api_key)
            models = list(client.models.list())
            check(f"{label} authentication", True, f"{len(models)} models available")
    except ImportError as exc:
        check(f"{label} authentication", False, f"Missing package: {exc} — run: pip install -r requirements.txt")
    except Exception as exc:
        check(f"{label} authentication", False, f"Unexpected error: {exc}")


def check_jira() -> None:
    print(f"\n{BOLD}=== Jira ==={RESET}")
    try:
        import requests
        from requests.auth import HTTPBasicAuth

        base = os.getenv("JIRA_BASE_URL", "").rstrip("/")
        auth = HTTPBasicAuth(os.getenv("JIRA_EMAIL", ""), os.getenv("JIRA_API_TOKEN", ""))
        resp = requests.get(f"{base}/rest/api/3/myself", auth=auth, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            check(
                "Jira authentication",
                True,
                f"Logged in as: {data.get('displayName', 'unknown')}",
            )
        elif resp.status_code == 401:
            check(
                "Jira authentication",
                False,
                "Invalid credentials — check JIRA_EMAIL and JIRA_API_TOKEN",
            )
        elif resp.status_code == 403:
            check(
                "Jira authentication",
                False,
                "Forbidden — your account may not have API access",
            )
        else:
            check(
                "Jira authentication",
                False,
                f"HTTP {resp.status_code}: {resp.text[:100]}",
            )
    except ImportError:
        check("Jira authentication", False, "requests package not installed")
    except requests.ConnectionError as exc:
        check(
            "Jira authentication",
            False,
            f"Could not connect to {os.getenv('JIRA_BASE_URL')}: {exc}",
        )
    except Exception as exc:
        check("Jira authentication", False, f"Unexpected error: {exc}")


def check_slack() -> None:
    print(f"\n{BOLD}=== Slack ==={RESET}")
    try:
        from slack_sdk import WebClient
        from slack_sdk.errors import SlackApiError

        client = WebClient(token=os.getenv("SLACK_BOT_TOKEN", ""))
        resp = client.auth_test()
        if resp["ok"]:
            check(
                "Slack authentication",
                True,
                f"Bot: {resp.get('bot_id', 'unknown')} in workspace {resp.get('team', 'unknown')}",
            )
            # Test channel access
            channel_id = os.getenv("SLACK_CHANNEL_ID", "")
            try:
                ch_resp = client.conversations_info(channel=channel_id)
                ch_name = ch_resp["channel"]["name"]
                check(
                    "Slack channel access",
                    True,
                    f"Channel: #{ch_name} ({channel_id})",
                )
            except SlackApiError as exc:
                err = exc.response["error"]
                if err == "channel_not_found":
                    check(
                        "Slack channel access",
                        False,
                        f"Channel {channel_id} not found — check SLACK_CHANNEL_ID and ensure bot is invited",
                    )
                else:
                    check("Slack channel access", False, f"Slack API error: {err}")
        else:
            check(
                "Slack authentication",
                False,
                f"auth_test failed: {resp.get('error')}",
            )
    except ImportError:
        check("Slack authentication", False, "slack_sdk package not installed")
    except SlackApiError as exc:
        err = exc.response["error"]
        if err in ("invalid_auth", "not_authed"):
            check(
                "Slack authentication",
                False,
                "Invalid bot token — check SLACK_BOT_TOKEN",
            )
        else:
            check("Slack authentication", False, f"Slack API error: {err}")
    except Exception as exc:
        check("Slack authentication", False, f"Unexpected error: {exc}")


def check_github() -> None:
    print(f"\n{BOLD}=== GitHub ==={RESET}")
    try:
        from github import Github, GithubException

        gh = Github(os.getenv("GITHUB_TOKEN", ""))
        user = gh.get_user()
        check("GitHub authentication", True, f"Logged in as: {user.login}")
        # Test repo access
        repo_name = os.getenv("GITHUB_REPO", "")
        try:
            repo = gh.get_repo(repo_name)
            check(
                "GitHub repo access",
                True,
                f"Repo: {repo.full_name} (default branch: {repo.default_branch})",
            )
        except GithubException as exc:
            if exc.status == 404:
                check(
                    "GitHub repo access",
                    False,
                    f"Repo '{repo_name}' not found — check GITHUB_REPO and token permissions",
                )
            elif exc.status == 401:
                check(
                    "GitHub repo access",
                    False,
                    "Unauthorized — check GITHUB_TOKEN",
                )
            else:
                check(
                    "GitHub repo access",
                    False,
                    f"GitHub API error {exc.status}: {exc.data}",
                )
    except ImportError:
        check("GitHub authentication", False, "PyGithub package not installed")
    except Exception as exc:
        check("GitHub authentication", False, f"Unexpected error: {exc}")


def main() -> int:
    print(f"{BOLD}agent-spike — Setup Validation{RESET}")
    print("=" * 50)

    env_ok = check_env_vars()

    if not env_ok:
        print(
            f"\n{RED}Fix missing environment variables before running auth checks.{RESET}"
        )
        print("   Edit .env and replace all placeholder values.\n")
        # Still attempt auth checks — some may work with partial config

    check_openai()
    check_jira()
    check_slack()
    check_github()

    print("\n" + "=" * 50)
    total = _pass_count + _fail_count
    if _fail_count == 0:
        print(f"{GREEN}{BOLD}All {total} checks passed. System is ready.{RESET}")
        return 0
    else:
        print(
            f"{RED}{BOLD}{_fail_count}/{total} checks failed. "
            f"Fix the issues above before running the orchestrator.{RESET}"
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
