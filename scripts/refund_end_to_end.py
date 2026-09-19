"""Golden-path PERJURY demo using a real mutation inside Modal.

This deliberately hard-codes one semantic mutation and one regression test.
The next milestone replaces those two hard-coded artifacts with Gemini output
while keeping the execution and verification path unchanged.
"""

from __future__ import annotations

from perjury.contracts import MutationStatus, VerificationEvidence
from perjury.modal_runner import RunSpec, execute_pytest
from perjury.verification import judge_verification

ORIGINAL_SOURCE = """\
def calculate_refund(price: float, days_since_purchase: int, premium: bool) -> float:
    \"\"\"Premium customers retain refund eligibility after the standard 30-day window.\"\"\"
    if price < 0:
        raise ValueError("price must be non-negative")
    if days_since_purchase < 0:
        raise ValueError("days_since_purchase must be non-negative")

    if days_since_purchase <= 30 or premium:
        return price
    return 0.0
"""

MUTANT_SOURCE = ORIGINAL_SOURCE.replace(
    "if days_since_purchase <= 30 or premium:",
    "if days_since_purchase <= 30:",
)

EXISTING_TESTS = """\
from examples.refund.refund import calculate_refund


def test_standard_customer_inside_window_gets_refund() -> None:
    assert calculate_refund(100.0, 14, premium=False) == 100.0


def test_standard_customer_outside_window_gets_no_refund() -> None:
    assert calculate_refund(100.0, 45, premium=False) == 0.0


def test_premium_customer_inside_window_gets_refund() -> None:
    assert calculate_refund(100.0, 14, premium=True) == 100.0


def test_zero_price_is_valid() -> None:
    assert calculate_refund(0.0, 10, premium=False) == 0.0
"""

GENERATED_TEST = """\


def test_premium_customer_outside_window_gets_refund() -> None:
    assert calculate_refund(100.0, 45, premium=True) == 100.0
"""


def files_for(source: str, tests: str) -> dict[str, str]:
    return {
        "examples/__init__.py": "",
        "examples/refund/__init__.py": "",
        "examples/refund/refund.py": source,
        "examples/refund/test_refund.py": tests,
    }


def run_case(mutation_id: str, source: str, tests: str):
    return execute_pytest(
        RunSpec(
            mutation_id=mutation_id,
            workspace_files=files_for(source, tests),
            command=("pytest", "-q", "examples/refund/test_refund.py"),
        )
    )


def main() -> None:
    print("1. Baseline: original implementation against existing tests")
    baseline = run_case("B00", ORIGINAL_SOURCE, EXISTING_TESTS)
    print(
        f"   exit={baseline.exit_code} duration={baseline.duration_ms}ms "
        f"status={baseline.status}"
    )
    if baseline.exit_code != 0:
        raise SystemExit("Baseline is not green; aborting mutation analysis.")

    print("\n2. Attack: remove premium refund eligibility")
    mutant = run_case("M01", MUTANT_SOURCE, EXISTING_TESTS)
    print(
        f"   exit={mutant.exit_code} duration={mutant.duration_ms}ms "
        f"status={mutant.status}"
    )
    if mutant.status is not MutationStatus.SURVIVED:
        raise SystemExit("Expected M01 to survive the incomplete test suite.")

    print("\n3. Proposed regression test")
    print(
        "   test_premium_customer_outside_window_gets_refund: "
        "premium=True, day=45 must still refund"
    )

    hardened_tests = EXISTING_TESTS + GENERATED_TEST

    print("\n4. Two-sided verification")
    original_with_test = run_case("V01-original", ORIGINAL_SOURCE, hardened_tests)
    mutant_with_test = run_case("V01-mutant", MUTANT_SOURCE, hardened_tests)

    evidence = VerificationEvidence(
        mutation_id="M01",
        original_exit_code=original_with_test.exit_code or 0,
        mutant_exit_code=mutant_with_test.exit_code or 0,
        original_stdout=original_with_test.stdout,
        mutant_stdout=mutant_with_test.stdout,
        original_duration_ms=original_with_test.duration_ms,
        mutant_duration_ms=mutant_with_test.duration_ms,
    )
    verdict = judge_verification(evidence, mutation_id="M01")

    print(f"   original + generated test: exit={original_with_test.exit_code}")
    print(f"   mutant   + generated test: exit={mutant_with_test.exit_code}")
    print(f"\nPERJURY VERDICT: {verdict.verdict.upper()}")
    print(verdict.explanation)

    if verdict.verdict != "verified":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
