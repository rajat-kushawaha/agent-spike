# BA Agent Knowledge Base

## Project Context

This is a sample project used to test an agentic workflow system. The codebase is a Python utility library with calculator and string manipulation functions. New features typically involve adding utility functions, fixing bugs, or extending existing functionality.

## Domain Terminology

- **Epic** — A large body of work containing multiple related tickets
- **Story** — A user-facing feature or requirement
- **Task** — A technical work item, often a subset of a story
- **Bug** — A defect in existing functionality
- **Spike** — A time-boxed exploration to learn something

## Common Ambiguities to Watch For

When analyzing tickets, these areas are frequently underspecified:

1. **Error handling** — What should happen on invalid input? Empty input? Boundary conditions?
2. **Edge cases** — Negative numbers, zero, very large values, unicode, empty strings
3. **Performance expectations** — Is this for batch processing or real-time?
4. **Integration points** — Does this interact with existing functions? Which ones?
5. **Testing expectations** — What level of test coverage is required?
6. **Backwards compatibility** — Should existing API contracts be preserved?
7. **Configuration** — Are values hardcoded or should they be configurable?
8. **Logging and observability** — Does this need new log statements?

## Stakeholder Communication Style

- Use clear, jargon-free language when communicating on Slack
- Use markdown formatting (bold, lists, code blocks) for readability
- Number questions when asking multiple
- Always include the Jira ticket key in messages for context
- Be concise — busy stakeholders appreciate short messages

## Acceptance Criteria Best Practices

Good acceptance criteria are:

- **Testable** — A QA engineer can verify them with concrete steps
- **Specific** — No vague terms like "fast" or "user-friendly"
- **Independent** — Each criterion stands alone

Convert vague requirements like "the function should be fast" into "the function should process 1000 items in under 100ms."
