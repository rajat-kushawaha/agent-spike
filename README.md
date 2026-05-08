# agent-spike — Multi-Agent Software Development Workflow

A fully automated multi-agent system that takes Jira tickets from "To Do" to merged pull request without human intervention. Three AI-powered agents (Business Analyst, Developer, Tech Lead) collaborate to analyse requirements, write code, run CI, review pull requests, and merge approved changes.

---

## What This System Does

1. **BA Agent** — Fetches "To Do" Jira tickets, produces structured implementation specifications using GPT-4o, and posts the analysis back to Jira as a comment.
2. **Developer Agent** — Picks up analysed tickets, generates implementation code, creates a GitHub branch and pull request, then waits for CI. If CI fails, it automatically retries up to `MAX_FIX_ATTEMPTS` times.
3. **Tech Lead Agent** — Reviews approved PRs by reading the diff and CI status, then either merges (squash) or requests changes. If changes are requested, the task is re-queued for the developer.

All three agents run in a continuous poll loop orchestrated by `orchestrator.py`.

---

## Prerequisites

- Python 3.11 or later
- An OpenAI account with API access (GPT-4o or GPT-4o-mini)
- A Jira Cloud project with API token access
- A Slack workspace with a bot app installed
- A GitHub repository with a Personal Access Token (`repo` scope)
- The `sample-repo-template/` contents pushed to your GitHub repo (see below)

---

## Installation

```bash
# Clone or download the project
cd /path/to/agent-spike

# Create a virtual environment
python -m venv .venv
source .venv/bin/activate   # On Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

---

## Configuration

Copy `.env.example` to `.env` (already done — `.env` is pre-populated with placeholders):

```bash
cp .env.example .env
```

Open `.env` and fill in every value:

| Variable | Description |
|---|---|
| `OPENAI_API_KEY` | Your OpenAI API key (starts with `sk-`) |
| `OPENAI_MODEL` | Model name, default `gpt-4o` (use `gpt-4o-mini` for lower cost) |
| `JIRA_BASE_URL` | Your Atlassian URL, e.g. `https://myorg.atlassian.net` |
| `JIRA_EMAIL` | Email of your Atlassian account |
| `JIRA_API_TOKEN` | API token from https://id.atlassian.com/manage-profile/security/api-tokens |
| `JIRA_PROJECT_KEY` | Project key in Jira, e.g. `DEV` |
| `SLACK_BOT_TOKEN` | Bot token starting with `xoxb-` |
| `SLACK_CHANNEL_ID` | Channel ID (starts with `C`, not the channel name) |
| `GITHUB_TOKEN` | Personal Access Token with `repo` scope |
| `GITHUB_REPO` | Format: `owner/repo-name` |
| `GITHUB_BASE_BRANCH` | Default branch, usually `main` |
| `POLL_INTERVAL_SECONDS` | How often the orchestrator polls (default: 60) |
| `MAX_FIX_ATTEMPTS` | Max CI fix retries per ticket (default: 3) |
| `STATE_FILE` | Path to the state file (default: `state.json`) |

---

## Setting Up the Sample Repository

The system is designed to operate on the `sample-repo-template/` project. Push it to GitHub as a new repository:

```bash
# Create a new repo on GitHub called (e.g.) "agent-spike-demo"
# Then push the sample template contents to it:

cd sample-repo-template
git init
git add .
git commit -m "Initial sample repo"
git remote add origin https://github.com/YOUR_ORG/YOUR_REPO.git
git push -u origin main
cd ..
```

Set `GITHUB_REPO=YOUR_ORG/YOUR_REPO` in `.env`.

---

## Validate Your Setup

Before running the orchestrator, verify all credentials are correct:

```bash
python validate_setup.py
```

This checks:
- All required `.env` variables are present and not placeholders
- OpenAI authentication works
- Jira authentication works and the project is accessible
- Slack bot token is valid and the channel is accessible
- GitHub token is valid and the repo is accessible

Fix any failures before proceeding — the orchestrator will error immediately if credentials are wrong.

---

## Creating Jira Tasks

Create tasks in your Jira project with status "To Do". The BA agent fetches all issues in this status.

See `docs/test-tasks.md` for three ready-to-use task descriptions:
- **Task 1** — Clear, low-complexity (add `divide()` to calculator)
- **Task 2** — Vague (tests the clarification flow)
- **Task 3** — Medium complexity (add `power()` and `square_root()`)

Copy the Summary and Description from that file into Jira. Set status to "To Do".

---

## Running the Orchestrator

### Full workflow (all agents in a loop):
```bash
python orchestrator.py
```

### Run once and exit:
```bash
python orchestrator.py --once
```

### Dry-run (no external API mutations — safe for testing):
```bash
python orchestrator.py --dry-run --once
```

### Verbose debug output:
```bash
python orchestrator.py --log-level DEBUG --once
```

The orchestrator will log each agent's activity to stdout. Press Ctrl+C to stop the loop.

---

## Testing Individual Agents

Use `--only-agent` to run a single agent per cycle. This is useful for phased testing:

```bash
# Step 1: Run only the BA agent to analyse your Jira tickets
python orchestrator.py --only-agent ba --once

# Step 2: Inspect state.json to verify BA output, then run the dev agent
python orchestrator.py --only-agent dev --once

# Step 3: After CI passes, run the tech lead agent to review and merge
python orchestrator.py --only-agent tech_lead --once
```

---

## Using --dry-run for Testing

`--dry-run` mode skips all external API writes (GitHub commits, Jira comments, Slack messages) but still reads from external APIs. It logs what it _would_ do instead.

```bash
python orchestrator.py --dry-run --once --log-level DEBUG
```

Note: In dry-run mode, the AI client also skips OpenAI calls and returns stub responses, so no API credits are consumed.

---

## Architecture Overview

```
orchestrator.py          Main loop — instantiates agents and drives cycles
    |
    +-- BAAgent          Reads Jira "To Do" tickets, calls AI for analysis,
    |   (ba_agent.py)    posts comment, updates state to BA_DONE
    |
    +-- DevAgent         Reads BA_DONE tasks, calls AI for code generation,
    |   (dev_agent.py)   creates GitHub branch + PR, waits for CI,
    |                    retries on failure (FIX_PENDING -> DEV_DONE)
    |
    +-- TechLeadAgent    Reads DEV_DONE tasks, fetches PR diff + CI status,
        (tech_lead_agent.py) calls AI for review, merges or re-queues
```

### Shared dependencies (constructed once in orchestrator.py):

| Module | Purpose |
|---|---|
| `config.py` | Load and validate all environment variables |
| `logger.py` | Consistent structured logging |
| `state_manager.py` | Atomic JSON file tracking task lifecycle |
| `ai_client.py` | OpenAI wrapper with retry and JSON extraction |
| `jira_client.py` | Jira REST API wrapper |
| `slack_client.py` | Slack Web API wrapper |
| `github_client.py` | GitHub REST API wrapper (PyGithub + requests) |

### Task lifecycle:

```
NEW -> BA_PENDING -> BA_DONE -> DEV_PENDING -> DEV_DONE -> REVIEW_PENDING
                                     ^                           |
                                     |                    APPROVE -> MERGED
                                     |
                              FIX_PENDING <-- REQUEST_CHANGES
                                     |
                                (max attempts) -> FAILED
```

### Prompt templates (`prompts/`):

| File | Used by |
|---|---|
| `ba_analysis.txt` | BA Agent — initial ticket analysis |
| `ba_clarification.txt` | BA Agent — answers clarification questions |
| `dev_implementation.txt` | Dev Agent — generates implementation code |
| `dev_fix.txt` | Dev Agent — fixes CI failures |
| `tech_lead_review.txt` | Tech Lead Agent — PR review |

---

## Implementation Notes

### State durability
State is written atomically (write to `.tmp`, then `os.replace`) so a crash mid-write never produces a corrupt `state.json`. On restart, the orchestrator resumes from where it left off.

### Error isolation
Each agent wraps per-task processing in a try/except so one bad ticket never crashes the entire cycle. Failed tasks are marked `FAILED` in state and skipped on subsequent cycles.

### CI polling
The dev agent polls GitHub CI every 30 seconds with a 10-minute timeout. Adjust `_CI_POLL_INTERVAL` and `_CI_MAX_WAIT` in `github_client.py` if your CI is faster or slower.

### Token limit handling
The tech lead prompt truncates PR diffs to 8,000 characters to stay within GPT-4o's context window. For very large PRs, consider increasing this limit or switching to a model with a larger context window.

### Jira transitions
Jira workflow transition names vary by project configuration. If you see `WARNING Transition 'In Progress' not found`, check your Jira project's workflow and update the transition names in the agent code or in your Jira project settings. See `docs/troubleshooting.md` for details.

### Branch naming
The dev agent uses the branch name returned by the AI (e.g., `feature/dev-42-add-divide`). If the branch already exists on GitHub, it is reused rather than recreated.

### Secrets in .env
The `.gitignore` excludes `.env` from version control. Never commit your `.env` file. Always use `.env.example` as the template and keep `.env` local.
