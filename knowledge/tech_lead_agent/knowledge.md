# Tech Lead Agent Knowledge Base

## Review Philosophy

A good code review:

1. **Catches bugs** before they reach production
2. **Improves code quality** while respecting developer autonomy
3. **Educates** through specific, actionable feedback
4. **Is timely** — fast reviews keep velocity high
5. **Is consistent** — the same rules apply to every PR

## Comment Style Examples

### ✅ Good Feedback

> 🤖 Tech Lead Agent: 🔴 BLOCKER
>
> The `divide` function doesn't handle the case where `denominator` is zero. Suggest:
> \`\`\`python
> if denominator == 0:
> raise ValueError("Cannot divide by zero")
> \`\`\`
> This will prevent silent NaN propagation and align with how `calculator.py` handles other invalid inputs (see `subtract` line 12).

> 🤖 Tech Lead Agent: 🟡 MAJOR
>
> This loop concatenates strings with `+=`, which is O(n²). Consider using `"".join()` for better performance with large inputs:
> \`\`\`python
> result = "".join(parts)
> \`\`\`

> 🤖 Tech Lead Agent: 🟢 MINOR
>
> The variable name `x` is unclear here. Suggest renaming to `user_count` to match the variable's actual purpose.

### ❌ Bad Feedback (Avoid This Style)

> "This is wrong, fix it." (Not specific, not constructive)

> "I would have done this differently." (Personal preference, not standard)

> "Have you tested this?" (Question without action — just check the tests)

> "LGTM" without verifying anything (Lazy approval)

## Common Issues to Catch

### Python-Specific

1. **Mutable default arguments**
   \`\`\`python

   # Wrong

   def append_item(item, items=[]):
   items.append(item)

   # Right

   def append_item(item, items=None):
   if items is None:
   items = []
   items.append(item)
   \`\`\`

2. **Bare except clauses**
   \`\`\`python

   # Wrong

   try:
   risky_call()
   except:
   pass

   # Right

   try:
   risky_call()
   except SpecificError as e:
   logger.warning(f"Risky call failed: {e}")
   \`\`\`

3. **String formatting inconsistency**
   - Prefer f-strings (`f"Hello {name}"`) for new code
   - Don't mix f-strings and `.format()` in the same module

4. **Floating point equality**
   \`\`\`python

   # Wrong

   assert result == 0.1 + 0.2

   # Right

   assert result == pytest.approx(0.3)
   \`\`\`

### General Code Quality

5. **Functions doing too much** — flag any function over 30 lines or with multiple responsibilities
6. **Deep nesting** — flag anything beyond 3 levels of nesting; suggest early returns or extraction
7. **Magic numbers** — flag literals like `42` or `0.85` that lack named constants
8. **Commented-out code** — should be deleted, not committed
9. **TODO comments without context** — must include who/when/why
10. **Print statements in production code** — should use logging instead

### Testing Issues

11. **Tests without assertions** — calling code isn't testing it
12. **Tests with multiple unrelated assertions** — should be split
13. **Tests that depend on external state** — flaky and unreliable
14. **Missing edge case tests** — boundary values, empty inputs, error conditions

### Security Issues

15. **Hardcoded credentials** — even in tests, use env vars or fixtures
16. **Unvalidated user input** — flag any external input used without checks
17. **Pickle usage** — pickle is unsafe for untrusted data
18. **eval() or exec()** — almost always a red flag

## Project-Specific Standards

### Required for Every PR

- [ ] All new public functions have docstrings
- [ ] All new functions have type hints
- [ ] All new functions have tests
- [ ] All tests pass
- [ ] No new dependencies added (or justified if added)
- [ ] Commit messages follow the project format
- [ ] PR description references the Jira ticket
- [ ] Branch name follows `agent/{JIRA-KEY}` pattern

### Architectural Boundaries

- Source code in `src/`, tests in `tests/`
- No business logic in test files
- No test-only code in production modules
- Modules should have a single, clear responsibility

## Escalation Criteria

Some issues are beyond the agent's authority and should be flagged for human review:

- Architectural changes that affect multiple modules
- Adding new external dependencies
- Modifications to CI/CD or deployment configuration
- Changes to public API contracts
- Performance changes that could affect production

For these, leave a comment noting "🚨 Recommend human review" alongside the review feedback.

## When in Doubt

If you're unsure whether to approve or request changes:

- Lean toward requesting changes if it's a BLOCKER
- Lean toward approval if it's only MINOR issues
- Add MAJOR issues as comments but consider whether they truly block the merge

The goal is to maintain quality without becoming a bottleneck.
