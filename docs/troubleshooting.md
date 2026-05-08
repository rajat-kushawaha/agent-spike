# Troubleshooting Guide

## Authentication Errors

### OpenAI: `AuthenticationError`
**Symptom:** `openai.AuthenticationError: Incorrect API key provided`
**Fix:**
1. Check `OPENAI_API_KEY` in `.env` — should start with `sk-`
2. Verify the key at https://platform.openai.com/api-keys
3. Ensure you have a positive credit balance

### Jira: `401 Unauthorized`
**Symptom:** Jira client returns HTTP 401
**Fix:**
1. `JIRA_EMAIL` must be the email address on your Atlassian account
2. `JIRA_API_TOKEN` must be an API token (not your password) — generate one at https://id.atlassian.com/manage-profile/security/api-tokens
3. `JIRA_BASE_URL` must include `https://` and end without a slash, e.g. `https://myorg.atlassian.net`

### Jira: `403 Forbidden`
**Symptom:** Jira client returns HTTP 403
**Fix:**
- Your Atlassian account may be a guest. Ensure you have at least "Browse Projects" and "Create Issues" permissions on the project.

### Slack: `invalid_auth`
**Symptom:** `SlackApiError: The server responded with: {'ok': False, 'error': 'invalid_auth'}`
**Fix:**
1. `SLACK_BOT_TOKEN` must start with `xoxb-`
2. Regenerate at https://api.slack.com/apps -> your app -> OAuth & Permissions
3. Reinstall the app to your workspace after any scope changes

### Slack: `channel_not_found`
**Symptom:** `SlackApiError: {'error': 'channel_not_found'}`
**Fix:**
1. Invite the bot to the channel: `/invite @your-bot-name` in Slack
2. Verify `SLACK_CHANNEL_ID` — use the channel ID (starts with `C`), not the name
3. Find the channel ID: right-click the channel -> View channel details -> copy the ID at the bottom

### GitHub: `401 Bad credentials`
**Symptom:** `GithubException: 401 Bad credentials`
**Fix:**
1. Generate a new Personal Access Token at https://github.com/settings/tokens
2. Required scopes: `repo` (full control of private repositories)
3. For organisation repos, also enable SSO on the token if your org uses SSO

### GitHub: `404 Not Found` for repo
**Symptom:** `GithubException: 404 Not Found`
**Fix:**
1. `GITHUB_REPO` must be `owner/repo-name` (no `https://github.com/` prefix)
2. Ensure the token belongs to an account with access to the repo
3. For private repos, confirm the token has `repo` scope

---

## Branch Protection Blocking Merge

**Symptom:** `GithubException: 405 Method Not Allowed` or `"Required status check ... has not passed"` when the orchestrator tries to merge.

**Fix options (choose one):**
1. **Recommended for testing:** Disable branch protection on `main` temporarily while validating the system.
2. **Production setup:** Add the GitHub Actions bot as an "allowed merge" actor in branch protection rules, and ensure `GITHUB_BASE_BRANCH` CI checks are configured as required status checks.
3. If you use required reviews: temporarily disable "Require pull request reviews before merging" or add the token owner as an approved reviewer.

---

## CI Failures

### Tests fail because the AI wrote incorrect code
The dev agent will automatically retry up to `MAX_FIX_ATTEMPTS` times. Check the state.json file to see current fix attempt count. If the agent keeps failing, check the CI failure logs in the GitHub Actions tab.

### CI is stuck in `pending` state
**Symptom:** Orchestrator logs `CI still pending...` for more than 10 minutes.
**Possible causes:**
- GitHub Actions queue is backed up (check https://githubstatus.com)
- The workflow file has a syntax error — check `.github/workflows/ci.yml` in your repo
- The branch has no workflow file — ensure you copied the full `sample-repo-template/` contents

### `CI timed out` warning in logs
The orchestrator waited 10 minutes (`_CI_MAX_WAIT = 600`) without a terminal status. The task will be set to `FIX_PENDING`. Increase `_CI_MAX_WAIT` in `github_client.py` if your CI consistently takes longer.

---

## State File Issues

### `state.json` is corrupt
**Symptom:** `WARNING State file corrupt, starting fresh`
**Fix:** Delete `state.json` and restart the orchestrator. Tasks will be re-processed from their current Jira status.

### Task is stuck in a non-terminal state
Check `state.json` and manually set the task's status field to `NEW` to reprocess from the beginning, or to `BA_DONE` to skip the BA phase.

---

## Bot Not in Channel (Slack)

Run in Slack:
```
/invite @your-bot-name
```
Or go to the channel settings -> Integrations -> Add an App.

---

## Jira Transition Not Found

**Symptom:** `WARNING Transition 'In Progress' not found for DEV-1. Available: [...]`

**Fix:** Jira transition names are case-sensitive and vary by workflow. Check the exact transition name in your Jira project:
1. Go to your project -> Project settings -> Workflows
2. Note the exact names of your transitions (e.g., "Start Progress" instead of "In Progress")
3. Update `jira_client.py` `transition_issue` calls in the agents or customise your Jira workflow to match.

---

## Orchestrator Crashes Immediately

**Symptom:** `EnvironmentError: Required environment variable 'X' is not set`
**Fix:** Run `python validate_setup.py` to identify all missing variables, then fill in `.env`.

---

## Running Out of OpenAI Tokens / Rate Limits

**Symptom:** `WARNING OpenAI rate limit hit, waiting Xs`
**Fix:**
1. The client will automatically retry up to 3 times with exponential backoff.
2. Increase `POLL_INTERVAL_SECONDS` in `.env` to reduce API call frequency.
3. Upgrade your OpenAI plan for higher rate limits.
4. Switch to a smaller model: set `OPENAI_MODEL=gpt-4o-mini` in `.env` for development/testing.
