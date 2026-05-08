# Developer Agent Knowledge Base

## Codebase Overview

The repository is a Python utility library with the following structure:

\`\`\`
sample-repo/
├── src/
│ ├── **init**.py
│ ├── calculator.py # Arithmetic functions
│ └── string_utils.py # String manipulation functions
├── tests/
│ ├── test_calculator.py
│ └── test_string_utils.py
├── pytest.ini
└── requirements.txt
\`\`\`

## Coding Conventions

### File Organization

- Source code lives in `src/`
- Tests live in `tests/` and mirror the `src/` structure
- One module per logical concern (don't mix calculator and string utilities)

### Naming Conventions

- **Modules and files:** `snake_case` (e.g., `string_utils.py`)
- **Functions and variables:** `snake_case` (e.g., `calculate_total`)
- **Classes:** `PascalCase` (e.g., `UserProfile`)
- **Constants:** `UPPER_SNAKE_CASE` (e.g., `MAX_RETRIES`)
- **Private helpers:** prefix with underscore (e.g., `_internal_helper`)

### Function Patterns

\`\`\`python
def divide(numerator: float, denominator: float) -> float:
"""Divide two numbers with safety checks.

    Args:
        numerator: The number being divided.
        denominator: The number to divide by.

    Returns:
        The quotient of the division.

    Raises:
        ValueError: If denominator is zero.
    """
    if denominator == 0:
        raise ValueError("Cannot divide by zero")
    return numerator / denominator

\`\`\`

### Test Patterns

\`\`\`python
import pytest
from src.calculator import divide

def test_divide_returns_correct_quotient():
assert divide(10, 2) == 5.0

def test_divide_by_zero_raises_value_error():
with pytest.raises(ValueError, match="Cannot divide by zero"):
divide(10, 0)
\`\`\`

## Common Patterns to Follow

### Error Handling

- Validate inputs at the top of the function
- Raise specific exception types (`ValueError`, `TypeError`, custom exceptions)
- Include helpful error messages with context

### Default Arguments

- Never use mutable defaults (`def f(items=[])` is wrong; use `def f(items=None)` and assign inside)

### Imports

- Standard library imports first
- Third-party imports second
- Local imports last
- Each group separated by a blank line
- One import per line for clarity

### Logging

- Use Python's `logging` module, not `print()`
- Log at appropriate levels: `DEBUG`, `INFO`, `WARNING`, `ERROR`
- Include context in log messages

## Common Pitfalls to Avoid

1. **Off-by-one errors** in loops and slicing — write tests for boundary conditions
2. **Mutating function arguments** — return new objects instead
3. **Floating point comparisons** — use `pytest.approx()` or `math.isclose()`
4. **String concatenation in loops** — use `"".join()` instead
5. **Catching broad exceptions** — catch the specific exception you expect
6. **Ignoring return values** — if a function returns something, use it or document why not

## Dependencies

Current dependencies (see `requirements.txt`):

- `pytest` — testing framework

When adding a dependency:

- Justify it in the PR description
- Pin to a specific version
- Prefer standard library when reasonable

## Testing Standards

- **Minimum:** test the happy path of every public function
- **Recommended:** also test edge cases, error paths, and boundary conditions
- **Test files mirror source files:** `src/foo.py` → `tests/test_foo.py`
- **Tests must be independent** — order shouldn't matter
- **No real network calls or file I/O** in unit tests (use mocking)
