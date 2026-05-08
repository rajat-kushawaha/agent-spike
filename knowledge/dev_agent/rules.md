# Developer Agent Rules

## Mandatory Behaviors

### Code Quality

1. **Follow existing code style.** Match indentation, naming, and structural conventions of the surrounding code.
2. **Use type hints** on all function signatures (`def foo(x: int) -> str:`)
3. **Write docstrings** for all public functions and classes using Google or NumPy style
4. **Keep functions small** — single responsibility, ideally under 30 lines
5. **Use descriptive names** — `calculate_average_score` not `calc` or `do_thing`
6. **Avoid magic numbers** — assign meaningful constants
7. **Handle errors explicitly** — never use bare `except:` clauses

### Testing

8. **Always write tests** for new code. Coverage of the happy path is mandatory; edge cases are strongly encouraged.
9. **Run the full test suite** before pushing — never push code with failing tests
10. **Test names should describe behavior** — `test_divide_by_zero_raises_value_error` not `test_divide_2`
11. **One assertion per test** when practical — makes failures easier to diagnose
12. **Use pytest fixtures** for shared setup; avoid duplicating test setup code
13. **Java test files MUST go in `src/test/java/...`**, never in `src/main/java/...`. The test source root is always `src/test/java`. Example: `backend/src/test/java/com/example/auth/AuthControllerTest.java`

### Git Hygiene

13. **Branch name must follow the format** `agent/{JIRA-KEY}` — e.g., `agent/PROJ-123`
14. **Commit messages must follow this format:**
    \`\`\`
    {type}: {short description}

    {longer explanation if needed}

    Jira: {JIRA-KEY}
    \`\`\`
    Where `type` is one of: `feat`, `fix`, `refactor`, `test`, `docs`, `chore`

15. **Never commit secrets, credentials, or API keys**
16. **Never commit generated files** (cache, build artifacts, .env, etc.)
17. **Never modify files outside the scope of the ticket** unless absolutely necessary

### Pull Requests

18. **PR title format:** `[{JIRA-KEY}] {Ticket title}`
19. **PR description must include:**
    - Link to Jira ticket
    - Summary of what changed and why
    - List of files modified and rationale
    - Testing notes (what was tested, what wasn't)
    - Any deviations from the ticket requirements
20. **Mark the PR as draft** if any tests are skipped or known issues remain
21. **Tag yourself as the author** in the PR (the bot account)

### Responding to Review Feedback

22. **Address every review comment** — either by making the change or explaining why not
23. **Reply to each comment** acknowledging the change ("Done in commit abc123")
24. **Push fixes as new commits** — do not force-push during review
25. **Re-run all tests** after addressing feedback
26. **Continue iterating** until the Tech Lead approves — there is no retry limit

## Forbidden Behaviors

- Do not push to `main`, `master`, or any protected branch directly
- Do not add new dependencies (e.g., `pip install` packages) without strong justification
- Do not delete or rename existing files unless the ticket explicitly requires it
- Do not modify CI configuration unless the ticket is specifically about CI
- Do not include AI-generated code that you cannot explain or justify
- Do not skip writing tests with the excuse of "trivial change"
- Do not bypass linting, formatting, or type checking
