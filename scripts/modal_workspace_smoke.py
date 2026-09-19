"""Prove local/Modal parity for the canonical sanitized refund workspace."""

from __future__ import annotations

from pathlib import Path

from perjury.contracts import ExecutionOutcome
from perjury.modal_runner import ModalWorkspaceExecutor
from perjury.workspace import LocalPytestExecutor, refund_workspace_spec, run_baseline

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    spec = refund_workspace_spec(str(ROOT))

    print("Running local refund baseline through WorkspaceSpec...")
    local = run_baseline(spec, LocalPytestExecutor())
    print(
        f"local outcome={local.execution.outcome.value} "
        f"manifest={local.manifest_sha256} files={local.manifest.file_count}"
    )

    print("Running the exact sanitized refund snapshot in Modal...")
    remote = run_baseline(spec, ModalWorkspaceExecutor())
    print(
        f"modal outcome={remote.execution.outcome.value} "
        f"manifest={remote.manifest_sha256} files={remote.manifest.file_count} "
        f"duration={remote.execution.duration_ms}ms"
    )

    if local.execution.outcome is not ExecutionOutcome.PASS:
        raise SystemExit("FAIL: local refund baseline did not pass")
    if remote.execution.outcome is not ExecutionOutcome.PASS:
        raise SystemExit("FAIL: Modal refund baseline did not pass")
    if local.manifest.model_dump(mode="json") != remote.manifest.model_dump(mode="json"):
        raise SystemExit("FAIL: local and Modal baselines used different snapshot evidence")

    print("PASS: local and Modal baseline parity proven on the exact sanitized snapshot")


if __name__ == "__main__":
    main()
