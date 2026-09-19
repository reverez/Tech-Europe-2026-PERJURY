"""First hackathon gate: prove concurrent Modal sandbox execution before building UI.

Run after:
    pip install -e ".[dev]"
    modal setup
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from statistics import mean
from time import perf_counter

from perjury.modal_runner import RunSpec, execute_pytest

COUNT = 10


def main() -> None:
    specs = [
        RunSpec(
            mutation_id=f"M{i:02d}",
            command=("python", "-c", "print('ok')"),
        )
        for i in range(1, COUNT + 1)
    ]

    started = perf_counter()
    results = []

    with ThreadPoolExecutor(max_workers=COUNT) as pool:
        futures = [pool.submit(execute_pytest, spec) for spec in specs]
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            print(
                f"{result.mutation_id}: outcome={result.outcome} "
                f"exit={result.exit_code} duration={result.duration_ms}ms"
            )

    wall = perf_counter() - started
    print()
    print(f"sandboxes={len(results)} wall_clock={wall:.2f}s")
    print(f"mean_sandbox_duration={mean(r.duration_ms for r in results):.0f}ms")


if __name__ == "__main__":
    main()
