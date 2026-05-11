# Agent Spike — Build Log

A full record of how this multi-agent AI engineering system was built, every error hit, every design decision made, and every fix applied. Written for a future blog post.

---

## What We Built

A fully autonomous software engineering pipeline where AI agents collaborate to deliver Jira tickets end-to-end:

```
Jira Backlog → BA Agent → Dev Agent → Tech Lead Agent → Ready for Merge
```

- **BA Agent** — picks up Backlog tickets, analyses requirements, asks clarifying questions on Slack, writes enriched acceptance criteria back to Jira, transitions ticket to In Progress
- **Dev Agent** — picks up analysed tickets, plans implementation in chunks, writes code + tests, commits to GitHub branch, raises PR. On review feedback, fixes all issues and pushes again
- **Tech Lead Agent** — reviews PRs against acceptance criteria, either approves (GitHub approval + Jira transition to Ready for Merge) or requests changes with specific issues. Never merges — human does that

Multiple instances of each agent can run in parallel across terminals. Distributed file locking prevents two instances from processing the same ticket simultaneously.

---

## Phase 1 — Getting the First Run Working

### The Starting Point

Started with a skeleton codebase: config, basic Jira/Slack/GitHub clients, an orchestrator, and prompt files. Goal: get the BA agent to run end-to-end against real Jira/Slack/GitHub.

### Error 1 — OpenAI API Key Validation Failing

**Symptom:** `validate_setup.py` failed with an OpenAI API key error even though we were using OpenRouter.

**Root cause:** The validation script was hitting `api.openai.com/v1/models` to check the key, but the key was an OpenRouter key (`sk-or-v1-...`), not an OpenAI key.

**Fix:** Updated `validate_setup.py` to hit OpenRouter's own validation endpoint (`https://openrouter.ai/auth/key`) instead. Added `OPENAI_BASE_URL` to config and passed it as `base_url` to the `openai.OpenAI()` client constructor.

```python
# Before
client = openai.OpenAI(api_key=config.openai_api_key)

# After
client = openai.OpenAI(
    api_key=config.openai_api_key,
    base_url=config.openai_base_url,  # https://openrouter.ai/api/v1
)
```

---

### Error 2 — Jira Search API returning 410 Gone

**Symptom:** `GET /rest/api/3/search` returned HTTP 410.

**Root cause:** Atlassian deprecated the old search endpoint. The new endpoint is `/rest/api/3/search/jql`.

**Fix:** Updated `jira_client.py` to use the new endpoint across all JQL queries.

---

### Error 3 — Tickets in "BackLog" Not Found

**Symptom:** BA agent found no tickets even though Jira had tickets in Backlog status.

**Root cause:** JQL was filtering for `status = "To Do"` but the board used `"BackLog"` (capital L) as the status name.

**Fix:** Made the Jira status names fully configurable via `.env`:
```
JIRA_STATUS_BACKLOG=BackLog
JIRA_STATUS_IN_PROGRESS=In Progress
JIRA_STATUS_IN_REVIEW=In Review
JIRA_STATUS_READY_FOR_MERGE=Ready for Merge
```

---

### Error 4 — CI Polling Infinite Loop

**Symptom:** Dev agent raised a PR, then polled for CI status forever. The project had no CI configured.

**Root cause:** `get_ci_status()` only returned `"success"` if GitHub reported check runs or commit statuses. With no CI, it returned `"pending"` forever.

**Fix:** Added a check — if GitHub returns zero check runs AND zero commit statuses, treat it as no CI configured and return `"success"` immediately.

```python
if not check_runs and not statuses:
    return "success"  # no CI configured — treat as passing
```

---

### Error 5 — GitHub 422 Self-Review

**Symptom:** Tech Lead agent tried to approve or request changes via `create_review(event="REQUEST_CHANGES")` and got HTTP 422.

**Root cause:** GitHub does not allow the PR author to submit a formal review on their own PR. The bot account that created the PR was the same account trying to review it.

**Fix:** Changed `post_review_comment` to use `create_issue_comment()` (plain comment) instead of `create_review()`. For approvals, used a different GitHub account token or accepted that the approval comment would appear as a regular comment.

---

## Phase 2 — Major Workflow Redesign

After the basic run worked, the workflow was redesigned to be more realistic and production-like.

### Design Decisions Made

**BA Agent:**
- Only picks up tickets in the configured Backlog status (not In Progress or beyond)
- Appends its analysis to the Jira description using ADF (Atlassian Document Format) — does NOT replace the original description
- If clarification is needed, posts to a dedicated `#agent-clarifications` Slack channel and waits (real poll loop, does not self-answer)
- Transitions ticket to In Progress only after analysis is complete

**Dev Agent:**
- Plans implementation in logical chunks (up to 8), each covering specific acceptance criteria
- Commits one chunk at a time so each subsequent chunk has full context of what's already been written
- Runs a self-check after all chunks: "are ALL acceptance criteria covered?"
- Only raises a PR after the self-check passes — never raises a partial PR
- On review feedback: fixes ALL accumulated issues in one pass, not incrementally

**Tech Lead Agent:**
- Reviews against BA acceptance criteria + knowledge rules
- SHA-based dedup: records the last reviewed commit SHA and skips re-review if Dev hasn't pushed anything new
- Unlimited review rounds — no max cap
- Never merges — human merges manually
- Accumulates feedback across rounds with round labels so Dev sees the full history

---

### Error 6 — Slack `not_in_channel` on Thread Polling

**Symptom:** BA agent posted clarification to the main Slack channel but couldn't poll replies — bot returned `not_in_channel`.

**Root cause:** The bot was not a member of the channel it was posting clarifications to, so `conversations_replies` failed.

**Fix:** Added `SLACK_CLARIFICATION_CHANNEL_ID` config pointing to `#agent-clarifications`, a channel the bot was already in. All clarification posts and polls use that channel.

---

### Error 7 — BA Agent Not Waiting for Slack Replies

**Symptom:** BA agent was posting the clarification question then immediately moving the ticket forward without waiting for a human reply.

**Root cause:** Original design had the BA agent self-answer or skip the wait. Real clarification means pausing.

**Fix:** Introduced `BA_AWAITING_CLARIFICATION` status. BA agent sets this, releases the lock, and exits. On the next poll cycle, Phase 1 of `run()` checks for tickets in this status and polls `conversations_replies` for the thread. Only when a reply exists does it incorporate the answer and call `_finalise()`.

---

### Error 8 — Jira Description Being Overwritten

**Symptom:** BA agent's enriched description replaced the original ticket description entirely.

**Root cause:** `update_issue` was sending a fresh ADF document.

**Fix:** Implemented `append_to_description()` in `jira_client.py` that:
1. Fetches the current ADF description
2. Appends a horizontal rule (`{"type": "rule"}`)
3. Appends the new content as new paragraph nodes
4. PUTs the merged ADF back

---

### Error 9 — Truncated JSON from Large Tickets

**Symptom:** Large tickets (many acceptance criteria, lots of files) caused the AI to return truncated JSON that couldn't be parsed.

**Root cause:** Default `max_tokens` was too low. The model hit the limit mid-JSON.

**Fix:** Set `max_tokens=16000`. Added a continuation loop in `ai_client.py`:
- If `finish_reason == "length"`, send the partial response back as an assistant message and ask the model to continue
- Retry up to 3 times
- After accumulation, attempt JSON repair on the result if it's still malformed

```python
_MAX_TOKENS = 16000
_MAX_CONTINUATIONS = 3

if choice.finish_reason == "length":
    messages = messages + [
        {"role": "assistant", "content": accumulated},
        {"role": "user", "content": "Continue exactly from where you left off..."}
    ]
```

---

## Phase 3 — Multi-Process Scaling

### Design: Multiple Agent Instances in Parallel

Goal: run multiple Dev agent instances across terminals so they pick up different tickets simultaneously. BA and Tech Lead run as single instances.

### Problem — Race Conditions Between Instances

**Symptom:** Two Dev agent instances would both claim the same ticket and both run implementation, producing duplicate commits.

**Root cause:** State was read once at startup and cached in memory. Two processes read the same `BA_DESCRIPTION_UPDATED` status before either had a chance to update it.

**Fix — Distributed File Locking:**

Rewrote `state_manager.py` with atomic OS-level locking:
- Lock files stored in `locks/` directory
- `claim_task()` uses `os.open(path, O_CREAT | O_EXCL | O_WRONLY)` — atomic on POSIX, fails if file exists
- Lock file contains agent name, PID, and timestamp
- Stale locks (older than TTL) are cleaned up automatically
- `release_task()` deletes the lock file
- All read methods (`get_task`, `all_tasks`) re-read `state.json` fresh from disk on every call — no in-memory caching

**Additional fix — re-read status after acquiring lock:**

Even with file locking, two instances could race past the status check before either wrote the lock. Added a status re-read immediately after `claim_task()` succeeds:

```python
if not self._state.claim_task(task_id, "dev_agent"):
    continue
# Re-read after lock — another instance may have changed status
fresh_task = self._state.get_task(task_id) or task
fresh_status = fresh_task.get("status")
if fresh_status not in _TRIGGER_STATUSES:
    continue  # already taken
```

---

### Error 10 — Multiple Commits Per Chunk (GitHub Contents API)

**Symptom:** Each implementation chunk that touched 5 files produced 5 separate commits instead of 1.

**Root cause:** `commit_files()` was using `repo.create_file()` / `repo.update_file()` in a loop — the GitHub Contents API creates one commit per file.

**Fix:** Rewrote `commit_files()` to use the **Git Trees API**:
1. Create a blob for each file
2. Build a tree with all blobs
3. Create a single commit pointing to the new tree
4. Update the branch ref

Result: any number of files → exactly one commit.

---

### Error 11 — Same Dev+TechLead Cycle Race

**Symptom:** Dev would push a fix, the orchestrator would immediately pass control to Tech Lead which would review the same commit that was just pushed — before Dev's commit was even visible.

**Fix:** Added `skip_tickets` to `TechLeadAgent.run()`. The orchestrator passes the set of ticket IDs that Dev just processed:

```python
just_pushed = set(dev.run())
tl.run(skip_tickets=just_pushed)
```

Tech Lead logs a skip message and the ticket stays in `PR_RAISED` for the next cycle.

---

## Phase 4 — Review Loop Bugs

### Problem — Dev Agent Not Fixing All Issues

**Symptom:** Tech Lead raised 7 issues. Dev committed a "fix" but the same issues came back next round.

**Root causes:**
1. `_fix()` was fetching files from `ba_analysis.files_to_change` (original BA list) instead of `submitted_files` (actual files on the branch). Files added during chunked implementation weren't being sent to the AI.
2. The fix prompt had a hardcoded `"commit_message": "fix({ticket_id}): address all tech lead feedback"` as an example — the AI copied it literally every time instead of generating a descriptive message.
3. When AI returned no files, the agent silently marked `PR_RAISED` anyway instead of retrying.

**Fixes:**
- Fetch from `submitted_files` keys (actual branch) not BA's file list
- Changed `commit_message` example in prompt to require a descriptive summary
- Changed empty files response to raise an exception so the ticket stays in `CHANGES_REQUESTED` and retries next cycle

---

### Problem — Feedback Accumulation Becoming Noise

**Symptom:** After 7 rounds, the ticket had 45 feedback items. Many were the same issue phrased differently across rounds. The AI was overwhelmed and kept making partial fixes.

**Fix:** Added deduplication in `_fix()` — strips `[Round N]` prefix and collapses items with the same first 80 characters before sending to the AI.

---

### Problem — Tech Lead Repeating the Same Issues Every Round

**Symptom:** Issues like "missing type hints" and "use a database instead of JSON" appeared in every single review comment, even after being addressed or being intentional design decisions.

**Root causes:**
1. Tech Lead had no memory of what it had already raised
2. Tech Lead was applying Python-specific rules (type hints, bare except) to Java code
3. Tech Lead was flagging "use a database" even though the BA spec explicitly said `data/users.json`

**Fixes:**
- Passed `prior_feedback` as a template variable to the review prompt — Tech Lead sees all previous issues and is instructed not to repeat them
- Added language-awareness instruction: "apply standards appropriate to the language in the diff"
- Added `_filter_spec_contradictions()` in the Tech Lead agent — a code-level post-processor that:
  - Detects when the BA spec chose a specific storage approach (e.g. JSON files)
  - Strips any review issue that challenges that decision
  - Moves it to suggestions instead
- Added repeat-issue demotion: any issue raised 3+ times gets moved to suggestions automatically

---

### Problem — Tech Lead Reviewing 3 Times Simultaneously

**Symptom:** Three identical review comments appeared on a PR within minutes of each other.

**Root cause:** The SHA dedup (`last_reviewed_sha`) was written *after* the review was posted. In the time between starting the review and writing the SHA, the same instance (or another instance) could start a second review of the same commit.

**Fix:** Write `last_reviewed_sha` to state *before* calling the AI — at the very start of the review, alongside setting status to `UNDER_REVIEW`. Any subsequent poll immediately sees the SHA matches and skips.

```python
# Before (SHA written after review)
review = self._ai.complete(...)
self._state.upsert_task(ticket_id, {"review": review, "last_reviewed_sha": current_sha})

# After (SHA written before review starts)
self._state.upsert_task(ticket_id, {"status": "UNDER_REVIEW", "last_reviewed_sha": current_sha})
review = self._ai.complete(...)
```

---

### Problem — AuthControllerTest.java in Wrong Directory

**Symptom:** Tech Lead kept saying "AuthControllerTest.java is missing" even though Dev claimed to have written it.

**Root cause:** Dev agent was creating the test file at `backend/src/main/java/com/example/auth/AuthControllerTest.java` instead of `backend/src/test/java/com/example/auth/AuthControllerTest.java`. The test source root in a Java Maven/Gradle project is `src/test/java`, not `src/main/java`.

**Fix:**
- Manually moved the file on the branch using the Git Trees API
- Added an explicit rule to `knowledge/dev_agent/rules.md`:
  > Java test files MUST go in `src/test/java/...`, never in `src/main/java/...`

---

### Problem — BA Agent Re-Analysing Already-Processed Tickets

**Symptom:** BA agent would re-pick up a ticket that Dev had already started working on (status was `BA_DESCRIPTION_UPDATED`) and re-run BA analysis, overwriting the state and resetting the ticket.

**Root cause:** Jira still showed the ticket as `BackLog` (BA transitions it to In Progress, but sometimes there's a delay). On the next poll, BA fetched it from Jira again and saw no state entry (or a state entry without the BA-done statuses).

**Fix:** Added a check in BA agent — if `state.get_task(ticket_id)` already has a `ba_analysis` key, skip the ticket regardless of Jira status:

```python
if self._state.get_task(ticket_id) and self._state.get_task(ticket_id).get("ba_analysis"):
    log.debug("Skipping %s — already has ba_analysis in state", ticket_id)
    continue
```

---

## Model Choices

### What We Tried
- Started with `openai/gpt-4o` on OpenRouter — works well, expensive
- Tried `google/gemini-2.5-flash-preview` — wrong model ID, got 400
- Tried `google/gemini-2.5-flash` — correct ID, but first call was slow and appeared stuck
- Reverted to `openai/gpt-4o` for reliability

### Recommendation for Production
- `google/gemini-2.5-flash` — best cost/quality ratio, 1M token context
- `openai/gpt-4o-mini` — good fallback, better JSON reliability than Gemini
- Mix models per agent: cheaper model for BA (structured analysis), stronger model for Tech Lead (code review judgment)

---

## Standalone Runners

Created three standalone runner scripts for multi-terminal operation:

```bash
# Terminal 1 — BA agent (single instance)
python run_ba.py --log-level DEBUG

# Terminal 2 — Dev agent (multiple instances OK, locking handles it)
python run_dev.py --log-level DEBUG

# Terminal 3 — Second Dev instance
python run_dev.py --log-level DEBUG

# Terminal 4 — Tech Lead (single instance recommended)
python run_tech_lead.py --log-level DEBUG
```

Each runner supports `--once` (single cycle then exit), `--dry-run` (log without executing API calls), and `--log-level`.

---

## Key Architecture Lessons

1. **Atomic locking is hard.** OS-level `O_CREAT|O_EXCL` works but you also need to re-read state *after* acquiring the lock, not just before.

2. **AI JSON output gets truncated.** Always set `max_tokens` high and build a continuation loop. Always have JSON repair as a fallback.

3. **Prompts need to be authoritative.** If you tell the AI "do X" but the example in the prompt shows Y, the AI copies Y. Examples must match instructions exactly.

4. **State machines need explicit terminal states.** `FAILED`, `APPROVED`, `IN_DEVELOPMENT` — agents must only pick up their own trigger statuses. Any state not in the trigger list is silently skipped.

5. **Review loops need memory.** Without passing prior feedback to the reviewer, it raises the same issues every round. The reviewer needs to know what it already said.

6. **Tech Lead needs to respect the spec.** An AI reviewer will flag "use a database" even when the spec says "use a JSON file". You need code-level post-processing to strip spec-contradicting issues, not just prompt instructions.

7. **GitHub Contents API = one commit per file.** Use the Git Trees API for atomic multi-file commits.

8. **Diff truncation causes wrong reviews.** If you only send 8000 chars of a large diff, the Tech Lead reviews an incomplete picture and flags missing code that exists outside the window. Bump the limit or summarise strategically.

---

## File Structure (as of May 2026)

```
agent-spike/
├── agents/
│   ├── ba_agent.py          # BA: analyse tickets, clarify on Slack, update Jira
│   ├── dev_agent.py         # Dev: plan → chunk → self-check → PR → fix loop
│   └── tech_lead_agent.py   # TL: review PR, approve or request changes
├── prompts/
│   ├── ba_analysis.txt      # BA analysis prompt
│   ├── ba_clarification.txt # BA clarification prompt
│   ├── dev_plan.txt         # Dev: break ticket into implementation chunks
│   ├── dev_implement_chunk.txt  # Dev: implement one chunk
│   ├── dev_self_check.txt   # Dev: verify all ACs covered before PR
│   ├── dev_fix.txt          # Dev: fix all Tech Lead feedback
│   └── tech_lead_review.txt # TL: review PR and decide APPROVE/REQUEST_CHANGES
├── knowledge/
│   ├── ba_agent/            # skills.md, rules.md, knowledge.md
│   ├── dev_agent/           # skills.md, rules.md, knowledge.md
│   └── tech_lead_agent/     # skills.md, rules.md, knowledge.md
├── ai_client.py             # OpenRouter/OpenAI wrapper with continuation loop
├── config.py                # All config from .env
├── github_client.py         # PyGithub wrapper, Git Trees API for commits
├── jira_client.py           # Jira REST API v3 wrapper
├── slack_client.py          # Slack bolt wrapper, clarification threading
├── state_manager.py         # JSON state + distributed file locking
├── orchestrator.py          # Single-process runner for all three agents
├── run_ba.py                # Standalone BA runner
├── run_dev.py               # Standalone Dev runner
├── run_tech_lead.py         # Standalone Tech Lead runner
├── knowledge_loader.py      # Loads per-agent knowledge files into system message
└── progress/
    └── progress.md          # This file
```

---

## What's Next / Known Issues

- **Model cost:** `gpt-4o` is expensive for an always-on polling system. Switch to `gemini-2.5-flash` once stability is confirmed.
- **Poll interval:** Currently 30 seconds. Could be event-driven (Jira webhook → queue) for lower latency and cost.
- **No CI enforcement:** Tech Lead is instructed to ignore CI. In a real project, you'd want the agent to wait for CI to pass before reviewing.
- **Human merge step:** By design, agents never merge. A human must click Merge on approved PRs.
- ~~**Stuck state recovery**~~ — fixed (see Phase 6).

---

## Phase 6 — Multi-Repo Support

### Feature — One Ticket Can Touch Multiple Repos

**Motivation:** Real projects have separate repos for API and UI (e.g. `revelio-api` and `revelio-ui`). A single Jira ticket may require changes in both.

**Design:**
- Added `GITHUB_REPOS` env var: comma-separated `name:owner/repo` pairs
  ```
  GITHUB_REPOS=api:rajat-gitting/revelio-api,ui:rajat-gitting/revelio-ui
  ```
- `config.py` parses this into a `github_repos: dict` (e.g. `{"api": "rajat-gitting/revelio-api", "ui": "..."}`)
- `GitHubClient` gains two new methods:
  - `for_repo(repo_name)` — returns a new client scoped to a specific repo
  - `resolve_repo(key)` — looks up a key from config and returns the scoped client
- BA analysis prompt now receives `AVAILABLE REPOSITORIES` and outputs `target_repos: list[str]` — the list of repo keys the ticket touches
- Each `files_to_change` entry now has a `repo` key so Dev knows which repo each file goes to
- Dev agent resolves all target repos at the start of `_implement`, creates the branch in each, routes each chunk's files to the correct repo client, and raises one PR per repo
- State stores `pr_numbers: dict` (key → PR number) alongside the primary `pr_number`

**Files changed:**
- `config.py` — added `github_repos` field and `_parse_github_repos()`
- `github_client.py` — added `for_repo()`, `resolve_repo()`
- `agents/ba_agent.py` — passes `available_repos` to BA analysis prompt
- `agents/dev_agent.py` — multi-repo branch creation, chunk routing, multi-PR creation
- `prompts/ba_analysis.txt` — added `AVAILABLE REPOSITORIES` section and `target_repos` field
- `.env` — added `GITHUB_REPOS` with keyword hints, removed `GITHUB_REPO`

### Improvement — Keyword-Based Repo Detection

**Problem:** Having both `GITHUB_REPO` (single default) and `GITHUB_REPOS` (multi-repo map) was confusing and redundant. More importantly, the BA agent was just passing repo names to the AI without any guidance on which tickets belong where — the AI had to guess.

**Fix:** Removed `GITHUB_REPO` entirely. `GITHUB_REPOS` now carries keywords per repo:
```
GITHUB_REPOS=api:rajat-gitting/revelio-api:api,backend,auth,endpoint,server,database,model,service;ui:rajat-gitting/revelio-ui:ui,frontend,react,component,page,screen,css,style
```

The BA analysis prompt receives these with their keywords:
```
- api: rajat-gitting/revelio-api (keywords: api, backend, auth, endpoint, ...)
- ui: rajat-gitting/revelio-ui (keywords: ui, frontend, react, component, ...)
```

The AI matches ticket content against keywords to determine `target_repos` automatically — no human tagging needed. A "user profile page with API endpoint" ticket correctly resolves to `["api", "ui"]`. To add a new repo, add one entry to `GITHUB_REPOS`.

### Fix — Chunk-Level Resume on Restart

**Problem:** If the Dev agent was stopped mid-implementation (e.g. after chunk 8 of 13), on restart it would re-plan from scratch and re-commit all chunks from chunk 1, creating duplicate commits on the branch.

**Fix:** Save implementation progress to state after every successful chunk commit:
- `impl_plan` — the full plan (chunks, PR title/body) saved after planning, before any commits
- `completed_chunks` — index of the last successfully committed chunk, incremented after each commit

On restart, if `impl_plan` and `completed_chunks > 0` are present in state, the agent skips re-planning and jumps straight to the next unfinished chunk. Already-committed chunks are skipped with a log message. Both fields are cleared when the PR is raised.

### Fix — Automatic Stuck Task Recovery on Startup

**Problem:** If any agent was stopped mid-cycle (Ctrl+C, crash, restart), the ticket would stay in `IN_DEVELOPMENT` or `UNDER_REVIEW` forever. No agent picks up those statuses — they're meant to be transient. Required manual `set_status` to unblock.

**Fix:** Added `StateManager.recover_stuck_tasks()` called at the top of each runner's `main()` before the poll loop starts. Recovery map:
- `IN_DEVELOPMENT` → `BA_DESCRIPTION_UPDATED` (Dev picks it up)
- `UNDER_REVIEW` → `PR_RAISED` (Tech Lead picks it up)
- `BA_ANALYZING` → removed from state (BA re-fetches from Jira)

Also cleans up stale lock files during recovery. Safe with multiple instances — only recovers tickets with no active lock (or an expired one).

### Improvement — Branch Names and Commit Message Format

**Branch naming:** Changed from `feature/kan-8-long-description` to just the ticket ID (e.g. `KAN-8`, `CR-1`). Enforced in code — `branch_name` is hardcoded to `ticket_id` in `dev_agent.py` regardless of what the AI returns in the plan.

**Commit messages:** All commits now follow `TICKET-ID | description` format (e.g. `CR-1 | add authentication endpoints and JWT middleware`). A `_fmt_commit()` helper in `dev_agent.py` wraps every commit call and strips any AI-generated prefix to avoid duplication like `CR-1 | CR-1 | message`.

---

## Phase 7 — Multi-Repo Routing Bugs

### Bug 1 — UI PR Not Raised (422 on Empty Branch)

**Symptom:** Dev raised the `api` PR successfully but crashed before recording state. Ticket marked `FAILED`. Tech Lead had nothing to pick up.

**Root cause:** PR creation looped over all repos — `api` succeeded, `ui` had no commits (all UI chunks were silrouted to `api`), `ui` threw 422, exception killed the whole task before state was written.

**Fix:** Wrapped each repo's `create_pull_request` in its own try/except. A repo with no commits logs a warning and is skipped — other repos' PRs are still recorded.

---

### Bug 2 — All Chunks Committed to Wrong Repo (`api` instead of `ui`)

**Symptom:** Dev agent did lots of UI work but made zero commits to `ui` repo. All UI files ended up in `api`.

**Root cause:** `repo_key` was never in the implement chunk prompt or the plan prompt. The AI never returned it. The fallback `github_clients.get("default", primary_gh)` always resolved to `primary_gh` (the `api` client).

**Fix (three-layer):**
1. `prompts/dev_plan.txt` — each chunk now requires `repo_key` matching the target repo. Instruction added: one repo per chunk, derive from `files_to_change[].repo` in the BA analysis.
2. `prompts/dev_implement_chunk.txt` — `repo_key` added to output schema.
3. `agents/dev_agent.py` — commit routing priority: `chunk["repo_key"]` (from plan) → BA path→repo map → primary. Plan value is set once before any implementation call, making it the most reliable source.

**Also fixed:** `_fix()` had the same bug — all fix commits went to `self._github` (api). Rewrote to group fixed files by repo using the same BA path→repo map and commit each group to the correct client.

---

### Bug 3 — Tech Lead Only Reviewed Primary (api) PR, Ignored ui PR

**Symptom:** Tech Lead reviewed `api` PR, posted comment, then on next cycle saw `last_reviewed_sha` matched and set status to `CHANGES_REQUESTED` — never looked at the `ui` PR.

**Root cause:** `_review_task` only used `task["pr_number"]` (single integer). No concept of iterating `pr_numbers` dict. SHA dedup was a single scalar `last_reviewed_sha`, not per-repo.

**Fix:** Rewrote `_review_task` to:
- Loop over all entries in `pr_numbers: dict[str, int]`
- Resolve the correct `GitHubClient` per repo via `resolve_repo(key)`
- Fetch only that repo's files (filtered by BA `path_to_repo` map)
- Track SHA dedup as `last_reviewed_shas: dict[str, str]` keyed by repo
- Collect all decisions — only `APPROVED` when every repo approves; `CHANGES_REQUESTED` if any repo needs changes
- Split `_approve`/`_request_changes` into `_approve_repo`/`_request_changes_repo` (per-PR actions) + `_finalise_approval`/`_finalise_changes_requested` (ticket-level state update)

---

## Phase 5 — Eliminating Phantom Review Comments

### Problem — Tech Lead Flagging Code That Was Already Implemented

**Symptom:** Tech Lead kept raising the same 2 issues every round (e.g. "FormController missing multi-step logic", "AuthControllerTest missing edge case tests") even after Dev had implemented them. The loop never converged.

**Root cause:** The Tech Lead was reviewing a truncated diff (first 16000 chars of raw unified diff). For large PRs, the files it was flagging existed and were correct — they just fell outside the diff window. The Tech Lead had no visibility into them and assumed they were missing.

**What we tried first:**
- Smart diff summariser (`get_pr_diff_smart`) — parsed diff into per-file sections, sorted by lines changed, budgeted 3000 chars per file. Better than raw truncation but still didn't solve the problem — a file could be summarised as "42 additions" with no actual content and the Tech Lead would still flag it.

**Final fix — send full file contents instead of the diff:**

The Tech Lead now fetches complete file contents from the branch for every file in `submitted_files`, and sends them all to the AI with no truncation. The AI client's continuation loop handles responses that exceed the token limit.

```python
# Before — truncated diff
pr_diff = self._github.get_pr_diff(pr_number)
# passed as pr_diff[:16000] to the prompt

# After — complete file contents from branch
submitted_keys = list(task.get("submitted_files", {}).keys())
branch_files = self._fetch_branch_files(branch_name, submitted_keys)
# passed in full to the prompt — no size limit
```

The prompt was also updated to make this explicit:
> "You have the COMPLETE file contents — do not assume anything is missing just because you don't see it in a diff."

**Files changed:**
- `agents/tech_lead_agent.py` — added `_fetch_branch_files()`, switched from `get_pr_diff_smart` to full file fetch
- `prompts/tech_lead_review.txt` — label changed from "DIFF" to "COMPLETE FILE CONTENTS ON THE BRANCH"
- `github_client.py` — `get_pr_diff_smart()` kept as fallback but no longer used in main flow

### Problem — Exposed API Key in git history

**Symptom:** `git push` to GitHub was rejected with `GH013: Repository rule violations found — Push cannot contain secrets`. The OpenRouter API key was embedded in `docs/test-tasks.md` line 57.

**Fix:**
1. Redacted the key in the file (replaced with `<YOUR_OPENROUTER_API_KEY>`)
2. Amended the single commit in history (`git commit --amend`)
3. Force-pushed to replace the commit on GitHub (`git push --force origin main`)
4. Rotated the API key — any key that was ever in a public repo should be considered compromised

**Lesson:** Never paste real API keys into docs, even in curl examples. Use placeholders from day one.

---

### Problem — Tech Lead reviewing cross-repo ACs (false BLOCKER flood)

**Symptom:** The Tech Lead agent was posting blockers like:
> "BLOCKER: No UI implementation exists. PR only contains Java backend code. Missing: HomePage.tsx, BlogCard.tsx, SkeletonCard.tsx..."

This happened on a backend-only PR for a ticket that also had a separate frontend PR. The agent was checking all acceptance criteria from the BA analysis (which covered both repos) against the backend code alone, flagging every frontend AC as a blocker.

**Root cause:** The `tech_lead_review.txt` prompt said "Verify every acceptance criterion" with no concept of repo scope. The AI had no way to know it was reviewing a backend-only PR and that frontend ACs would be covered in a separate review.

**Fix — two-part:**

1. **Prompt** (`prompts/tech_lead_review.txt`) — added `{repo_context}` variable and a new CRITICAL instruction at the top:
   > "Only evaluate acceptance criteria that can be verified from the files present in this PR. Do NOT flag missing frontend code when reviewing a backend PR, and vice versa."

2. **Agent** (`agents/tech_lead_agent.py`) — populate `repo_context` per repo in the review loop:
   - Single-repo ticket → "covers the full ticket scope"
   - Multi-repo ticket → lists the current repo and names the other repos, explicitly telling the AI not to flag code belonging to them

**Files changed:**
- `prompts/tech_lead_review.txt` — added `{repo_context}` section + scoped review instruction
- `agents/tech_lead_agent.py` — build `repo_context` string per repo before calling AI

---

### Problem — BA agent generating wrong file paths (Python paths for a Java repo)

**Symptom:** BA agent produced `src/routes/blogs.py`, `src/models/blog.py` for `revelio-api` — a Spring Boot Java/Gradle project. Dev agent then wrote Java code to those wrong paths, and some Java files ended up committed to `revelio-ui`.

**Root cause:** BA agent had no knowledge of actual repo structure or tech stack. It passed only repo names and keywords to the AI, which guessed Python (a common API pattern) for `revelio-api`.

**Fix:**
1. `github_client.py` — added `get_repo_structure(max_depth=2)` which fetches the real directory tree and primary language from GitHub
2. `agents/ba_agent.py` — now accepts a `GitHubClient`, calls `_fetch_repo_structures()` before analysis, injects real repo trees into the prompt
3. `prompts/ba_analysis.txt` — added `{repo_structures}` section and updated instruction 4: *"Use ACTUAL REPOSITORY STRUCTURES to derive real paths — never guess a language or path structure"*
4. `run_ba.py` + `orchestrator.py` — wired `GitHubClient` into `BAAgent` constructor

**Files changed:**
- `github_client.py` — `get_repo_structure()`
- `agents/ba_agent.py` — `_fetch_repo_structures()`, `GitHubClient` param
- `prompts/ba_analysis.txt` — `{repo_structures}` + instruction 4
- `run_ba.py`, `orchestrator.py` — constructor wiring

---

### Problem — Dev agent writing blind (no codebase awareness, 30% quality gap vs Claude Code)

**Symptom:** Dev agent was:
- Writing Java Spring Boot code with no `@RestController`/`@Service` annotations
- Overwriting `BlogCard.tsx` and dropping its `default export` and full props interface
- Not using existing hooks (`useApi`), types, or API client patterns already in the repo
- Skipping files from the BA plan entirely with no one catching it
- Tech lead approving PRs missing `HomePage.tsx`, `blogService.ts` etc.

**Root cause:** The dev agent wrote code blind — no knowledge of what already existed in the codebase. It never read files before writing them, never saw existing patterns, and the tech lead only reviewed what was submitted without checking what was planned but absent.

**Fix — 5 parts:**

1. **`agents/dev_agent.py` — `_fetch_repo_context()` (new method)**
   Before planning or writing any code, fetches per repo:
   - Key shared files (`useApi.ts`, `client.ts`, `endpoints.ts`, `ApiResponse.java`, `HealthController.java` etc.) — the patterns the AI must follow
   - Every planned file from `main` — so the AI knows what already exists
   - Sibling files in the same directories — for naming/export/import conventions
   Context injected into every prompt: planning, each chunk, self-check, and fix.

2. **`agents/dev_agent.py` — chunk loop + fix flow**
   Branch file fetches now use the correct `GitHubClient` per repo. Fix flow also fetches repo context before generating corrections.

3. **`prompts/dev_plan.txt`**
   Added: *"Use the same languages, frameworks, libraries already present. Reuse existing utilities, hooks, types — never duplicate them."*

4. **`prompts/dev_implement_chunk.txt`**
   Added `{repo_context}` section and rules: match imports/exports exactly, reuse existing utilities, preserve all existing code in modified files.

5. **`agents/tech_lead_agent.py` + `prompts/tech_lead_review.txt` — missing file check**
   Before each review, computes which BA-planned files are absent from the PR. Passes as `{missing_files}` — each missing file is an automatic blocker in the prompt. Catches silently skipped files like `HomePage.tsx` and `blogService.ts`.

**Files changed:**
- `agents/dev_agent.py` — `_fetch_repo_context()`, chunk loop, self-check, fix flow, `_fetch_branch_files_raw()` signature
- `prompts/dev_plan.txt` — pattern-following instructions
- `prompts/dev_implement_chunk.txt` — `{repo_context}` + convention rules
- `prompts/dev_fix.txt` — `{repo_context}`
- `agents/tech_lead_agent.py` — missing file detection, `missing_files` kwarg
- `prompts/tech_lead_review.txt` — `{missing_files}` section + blocker instruction

---

### Problem — Multiple one-off fixes to live repos (wrong annotations, wrong security config, missing deps)

**Symptoms caught post-merge:**
- `BlogController.java` missing `@RestController`, `@GetMapping`, `@RequestParam` — plain Java class, not an HTTP endpoint
- `BlogService.java` missing `@Service` — never registered with Spring, always returned empty list
- `SecurityConfig.java` — `/api/blogs` not in `permitAll`, blocked by Spring Security with 401
- `revelio-ui` — Java files (`BlogApiClient.java` etc.) committed to wrong repo
- `BlogCard.tsx` — `default export` dropped by CR-12 dev agent overwrite
- `date-fns` missing from `package.json`
- Slf4j import wrong: `lombok.extern.Slf4j` → `lombok.extern.slf4j.Slf4j`

**All fixed directly on `main` via GitHub API and squashed into single commits per repo following `CR-11 | fix(scope): description` pattern.**

---

### Problem — Jira config pointing at wrong project + wrong status names

**Symptom:** `JIRA_PROJECT_KEY=KAN` but board is `CR`. Status names `In Review` / `Ready for Merge` don't exist — board has `QA` / `UAT`.

**Fix:** Updated `.env`:
- `JIRA_PROJECT_KEY=CR`
- `JIRA_STATUS_IN_REVIEW=QA`
- `JIRA_STATUS_READY_FOR_MERGE=UAT`

---

### Problem — `ai_client.py` returning raw unformatted template on any key error

**Symptom:** Warning `"Prompt template key error (using partial sub): 'repo_structures'"` — when any placeholder was missing, the entire prompt was returned unformatted, so ALL substitutions failed.

**Fix:** `ai_client.py` — replaced bare `return template` fallback with a `_SafeDict` that substitutes known keys and replaces unknown ones with empty string, so the rest of the prompt still renders correctly.

---

### Problem — Dev agent `StopIteration` crash when `target_repos` is empty

**Symptom:** `BA analysis for CR-12 has no target_repos` — BA produced empty arrays due to malformed prompt. `next(iter(github_clients.values()))` raised `StopIteration`.

**Fix:** `agents/dev_agent.py` — added explicit guard: raises `ValueError` with actionable message when `target_repos` is empty, telling the operator to reset the ticket for re-analysis.

---

### Problem — Tech Lead moving ticket to wrong Jira status + self-approval 422 error

**Symptom 1:** Tech lead was transitioning tickets to `UAT` after approval, but the correct status is `In PR review` (new board column created for this). Human merges manually from `In PR review`.

**Fix:** `.env` — `JIRA_STATUS_READY_FOR_MERGE=In PR review`

**Symptom 2:** GitHub returned 422 `"Review Can not approve your own pull request"` on every approval because the same token creates the PR and tries to approve it. This was logged as ERROR and the pipeline continued, but no approval signal appeared on the PR.

**Fix:** `agents/tech_lead_agent.py` — `_approve_repo()` now tries the formal GitHub approval first. If it gets 422 (self-review blocked), it falls back to posting a plain PR comment `✅ Tech Lead Approval` with the review summary and a note that the PR is ready for manual merge. Degrades gracefully with no ERROR in logs.

**Files changed:**
- `.env` — `JIRA_STATUS_READY_FOR_MERGE=In PR review`
- `agents/tech_lead_agent.py` — `_approve_repo()` fallback to comment on 422
