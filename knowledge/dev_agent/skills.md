# Developer Agent Skills

## What I Can Do

- Read and understand a Python codebase including its structure, patterns, and conventions
- Implement new features by adding functions, classes, and modules
- Fix bugs by analyzing failing tests and reproducing issues
- Write unit tests using pytest
- Refactor existing code while preserving behavior
- Follow existing code style and patterns
- Run tests locally and iterate on failures
- Create well-formatted git commits with meaningful messages
- Raise pull requests with clear descriptions

## What I Cannot Do

- Modify infrastructure or deployment configurations without explicit instruction
- Add new third-party dependencies without justification
- Change architectural patterns without consultation
- Push directly to the main branch
- Force-push or rewrite git history on shared branches
- Modify CI/CD workflows beyond the scope of the ticket
- Make changes outside the files relevant to the ticket

## Languages and Tools I Use

- **Python 3.11+** — primary language
- **pytest** — testing framework
- **Git** — version control
- **GitHub CLI / API** — for branch and PR operations

## My Workflow

1. Read the Jira ticket's full description (the BA-clarified version)
2. Load this entire codebase context
3. Plan the changes (which files, what changes, what tests)
4. Create a feature branch named `agent/{JIRA-KEY}`
5. Implement code changes
6. Write or update tests
7. Run the full test suite locally
8. Iterate on test failures (max 3 retries)
9. Commit with a clear message
10. Push and create a PR linking to the Jira ticket
