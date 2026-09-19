"""Golden-path PERJURY demo using a real mutation inside Modal.

This deliberately hard-codes one semantic mutation and one regression test.
The next milestone replaces those two hard-coded artifacts with Gemini output
while keeping the execution and verification path unchanged.

This script is intentionally stricter than the legacy verifier: until issue #8
lands the semantic execution taxonomy, it accepts the golden path only when the
original exits exactly 0 and the mutant exits exactly 1.
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
from examples.refund.refund import calculate_refund


def test_premium_customer_outside_window_gets_refund() -> None:
    assert calculate_refund(100.0, 45, premium=True) == 100.0
"""

GENERATED_TEST_PATH = "examples/refund/test_perjury_M01.py"
EXISTING_TEST_PATH = "examples/refund/test_refund.py"


def files_for(source: str, *, include_generated_test: bool = False) -> dict[str, str]:
    files = {
        "examples/__init__.py": "",
        "examples/refund/__init__.py": "",
        "examples/refund/refund.py": source,
        EXISTING_TEST_PATH: EXISTING_TESTS,
    }
    if include_generated_test:
        files[GENERATED_TEST_PATH] = GENERATED_TEST
    return files


def run_case(
    mutation_id: str,
    source: str,
    *,
    include_generated_test: bool = False,
):
    test_paths = [EXISTING_TEST_PATH]
    if include_generated_test:
        test_paths.append(GENERATED_TEST_PATH)

    return execute_pytest(
        RunSpec(
            mutation_id=mutation_id,
            workspace_files=files_for(
                source,
                include_generated_test=include_generated_test,
            ),
            command=("pytest", "-q", *test_paths),
        )
    )


def main() -> None:
    print("1. Baseline: original implementation against existing tests")
    baseline = run_case("B00", ORIGINAL_SOURCE)
    print(
        f"   exit={baseline.exit_code} duration={baseline.duration_ms}ms "
        f"status={baseline.status}"
    )
    if baseline.exit_code != 0 or baseline.status is not MutationStatus.SURVIVED:
        raise SystemExit("Baseline is not a clean pytest pass; aborting mutation analysis.")

    print("\n2. Attack: remove premium refund eligibility")
    mutant = run_case("M01", MUTANT_SOURCE)
    print(
        f"   exit={mutant.exit_code} duration={mutant.duration_ms}ms "
        f"status={mutant.status}"
    )
    if mutant.exit_code != 0 or mutant.status is not MutationStatus.SURVIVED:
        raise SystemExit("Expected M01 to survive the incomplete test suite.")

    print("\n3. Proposed regression test")
    print(
        "   test_premium_customer_outside_window_gets_refund: "
        "premium=True, day=45 must still refund"
    )
    print(f"   candidate file: {GENERATED_TEST_PATH}")

    print("\n4. Two-sided verification")
    original_with_test = run_case(
        "V01-original",
        ORIGINAL_SOURCE,
        include_generated_test=True,
    )
    mutant_with_test = run_case(
        "V01-mutant",
        MUTANT_SOURCE,
        include_generated_test=True,
    )

    if original_with_test.exit_code != 0:
        raise SystemExit(
            "Original + generated test was not a clean pytest pass; refusing verification."
        )
    if mutant_with_test.exit_code != 1:
        raise SystemExit(
            "Mutant + generated test was not an ordinary pytest test failure; "
            "refusing verification."
        )

    evidence = VerificationEvidence(
        mutation_id="M01",
        original_exit_code=original_with_test.exit_code,
        mutant_exit_code=mutant_with_test.exit_code,
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
