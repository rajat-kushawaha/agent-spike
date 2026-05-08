# Test Task Descriptions

Copy and paste these into Jira as new "To Do" tasks in your project.

---

## Task 1 — Clear (Low Complexity)

**Summary:** Add a `divide` function to `calculator.py`

**Description:**
Add a `divide(a: float, b: float) -> float` function to `calculator.py`.

Requirements:

- The function must return `a / b` as a float
- If `b` is zero, raise a `ValueError` with the message `"Cannot divide by zero"`
- Add corresponding tests in `tests/test_calculator.py`:
  - `test_divide_positive()`: `divide(10, 2)` returns `5.0`
  - `test_divide_negative()`: `divide(-6, 2)` returns `-3.0`
  - `test_divide_by_zero()`: raises `ValueError`

This task is intentionally precise so you can verify the agents work correctly end-to-end.

---

## Task 2 — Vague (Medium Complexity)

**Summary:** Improve the string utilities

**Description:**
The `string_utils.py` file is missing some useful functions. Please add more string utility functions that developers would commonly need.

(Use this task to test the BA agent's clarification flow — it should ask follow-up questions and then answer them itself before producing a specification.)

---

## Task 3 — Medium Complexity

**Summary:** Add a `power` function and a `square_root` function to `calculator.py`

**Description:**
Extend `calculator.py` with two new mathematical functions:

1. `power(base: float, exponent: float) -> float`
   - Returns `base` raised to `exponent`
   - Must handle negative exponents correctly (e.g., `power(2, -1)` returns `0.5`)

2. `square_root(n: float) -> float`
   - Returns the square root of `n`
   - If `n` is negative, raise a `ValueError` with message `"Cannot compute square root of a negative number"`
   - Do not use `math.sqrt` — implement using `n ** 0.5`

Tests required for both functions in `tests/test_calculator.py`.

curl https://openrouter.ai/api/v1/auth/key \
 -H "Authorization: Bearer <YOUR_OPENROUTER_API_KEY>"
