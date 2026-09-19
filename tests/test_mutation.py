from __future__ import annotations

from pathlib import Path

import pytest

from perjury.contracts import MutationProposal, WorkspaceSpec
from perjury.mutation import (
    MutationAnchorError,
    StaleSnapshotError,
    UnsafeMutationPathError,
    UnsupportedMutationTargetError,
    apply_mutation,
)
from perjury.workspace import LocalPytestExecutor, run_baseline


def write_workspace(root: Path, *, source: str = "def value() -> int:\n    return 1\n") -> None:
    (root / "app.py").write_text(source, encoding="utf-8")
    (root / "test_app.py").write_text(
        "from app import value\n\n\ndef test_value() -> None:\n    assert value() == 1\n",
        encoding="utf-8",
    )


def spec_for(
    root: Path,
    *,
    mutable_paths: tuple[str, ...] = ("app.py",),
    context_paths: tuple[str, ...] = ("test_app.py",),
) -> WorkspaceSpec:
    return WorkspaceSpec(
        workspace_id="mutation-unit",
        source_root=str(root),
        snapshot_id="fixture:mutation-v1",
        pytest_argv=(
            "python",
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "test_app.py",
        ),
        mutable_paths=mutable_paths,
        context_paths=context_paths,
        environment={"PYTHONDONTWRITEBYTECODE": "1"},
    )


def proposal(
    *,
    file_path: str = "app.py",
    original: str = "return 1",
    mutated: str = "return 2",
    mutation_id: str = "M01",
) -> MutationProposal:
    return MutationProposal(
        id=mutation_id,
        file_path=file_path,
        description="change return value",
        hypothesis="existing tests may miss a behavioral branch",
        original_snippet=original,
        mutated_snippet=mutated,
    )


def baseline_for(root: Path, spec: WorkspaceSpec | None = None):
    active_spec = spec or spec_for(root)
    return active_spec, run_baseline(active_spec, LocalPytestExecutor())


def test_apply_mutation_changes_only_isolated_workspace(tmp_path: Path) -> None:
    write_workspace(tmp_path)
    spec, baseline = baseline_for(tmp_path)
    original_bytes = (tmp_path / "app.py").read_bytes()

    workspace = apply_mutation(spec, baseline, proposal())
    isolated_root = workspace.root
    try:
        assert (tmp_path / "app.py").read_bytes() == original_bytes
        assert "return 2" in (workspace.root / "app.py").read_text(encoding="utf-8")
        assert workspace.evidence.mutation_id == "M01"
        assert workspace.evidence.base_manifest_sha256 == baseline.manifest_sha256
        assert workspace.evidence.original_sha256 != workspace.evidence.mutated_sha256
        assert (
            workspace.evidence.mutated_manifest_sha256
            == workspace.manifest.manifest_sha256
        )
        assert workspace.evidence.mutated_manifest_sha256 != baseline.manifest_sha256
        assert "--- a/app.py" in workspace.evidence.diff
        assert "+++ b/app.py" in workspace.evidence.diff
        assert "-    return 1" in workspace.evidence.diff
        assert "+    return 2" in workspace.evidence.diff
    finally:
        workspace.cleanup()

    assert not isolated_root.exists()
    assert (tmp_path / "app.py").read_bytes() == original_bytes


def test_mutation_workspace_context_manager_cleans_up(tmp_path: Path) -> None:
    write_workspace(tmp_path)
    spec, baseline = baseline_for(tmp_path)

    with apply_mutation(spec, baseline, proposal()) as workspace:
        isolated_root = workspace.root
        assert isolated_root.exists()

    assert not isolated_root.exists()


def test_stale_source_manifest_is_rejected(tmp_path: Path) -> None:
    write_workspace(tmp_path)
    spec, baseline = baseline_for(tmp_path)
    (tmp_path / "app.py").write_text(
        "def value() -> int:\n    return 99\n",
        encoding="utf-8",
    )

    with pytest.raises(StaleSnapshotError, match="drifted"):
        apply_mutation(spec, baseline, proposal())


@pytest.mark.parametrize(
    "file_path",
    [
        "../app.py",
        "/tmp/app.py",
        "src/../../app.py",
        r"src\app.py",
    ],
)
def test_unsafe_mutation_paths_are_rejected(
    tmp_path: Path,
    file_path: str,
) -> None:
    write_workspace(tmp_path)
    spec, baseline = baseline_for(tmp_path)

    with pytest.raises(UnsafeMutationPathError):
        apply_mutation(spec, baseline, proposal(file_path=file_path))


def test_post_baseline_symlink_swap_is_rejected(tmp_path: Path) -> None:
    write_workspace(tmp_path)
    spec, baseline = baseline_for(tmp_path)
    outside = tmp_path.parent / "outside-perjury-target.py"
    outside.write_text("def value() -> int:\n    return 1\n", encoding="utf-8")
    (tmp_path / "app.py").unlink()
    (tmp_path / "app.py").symlink_to(outside)

    try:
        with pytest.raises(StaleSnapshotError, match="snapshot policy"):
            apply_mutation(spec, baseline, proposal())
    finally:
        (tmp_path / "app.py").unlink(missing_ok=True)
        outside.unlink(missing_ok=True)


def test_context_test_file_cannot_be_mutated(tmp_path: Path) -> None:
    write_workspace(tmp_path)
    spec, baseline = baseline_for(tmp_path)

    with pytest.raises(UnsafeMutationPathError, match="outside mutable"):
        apply_mutation(
            spec,
            baseline,
            proposal(
                file_path="test_app.py",
                original="assert value() == 1",
                mutated="assert value() == 2",
            ),
        )


def test_excluded_file_under_mutable_directory_cannot_be_mutated(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "src" / ".env").write_text("SECRET=bad\n", encoding="utf-8")
    (tmp_path / "test_app.py").write_text(
        "def test_ok() -> None:\n    assert True\n",
        encoding="utf-8",
    )
    spec = spec_for(
        tmp_path,
        mutable_paths=("src",),
        context_paths=("test_app.py",),
    )
    baseline = run_baseline(spec, LocalPytestExecutor())

    with pytest.raises(UnsupportedMutationTargetError, match="sanitized baseline manifest"):
        apply_mutation(
            spec,
            baseline,
            proposal(
                file_path="src/.env",
                original="SECRET=bad",
                mutated="SECRET=worse",
            ),
        )


@pytest.mark.parametrize(
    ("source", "original", "expected_matches"),
    [
        ("def value() -> int:\n    return 1\n", "missing anchor", 0),
        ("x = 1\nx = 1\n", "x = 1", 2),
    ],
)
def test_missing_or_ambiguous_anchor_is_rejected(
    tmp_path: Path,
    source: str,
    original: str,
    expected_matches: int,
) -> None:
    if "value" in source:
        write_workspace(tmp_path, source=source)
        spec, baseline = baseline_for(tmp_path)
    else:
        (tmp_path / "app.py").write_text(source, encoding="utf-8")
        (tmp_path / "test_app.py").write_text(
            "def test_ok() -> None:\n    assert True\n",
            encoding="utf-8",
        )
        spec, baseline = baseline_for(tmp_path)

    with pytest.raises(MutationAnchorError, match=f"found {expected_matches}"):
        apply_mutation(
            spec,
            baseline,
            proposal(original=original, mutated="replacement"),
        )


def test_noop_mutation_is_rejected(tmp_path: Path) -> None:
    write_workspace(tmp_path)
    spec, baseline = baseline_for(tmp_path)

    with pytest.raises(MutationAnchorError, match="no-op"):
        apply_mutation(
            spec,
            baseline,
            proposal(original="return 1", mutated="return 1"),
        )


def test_non_utf8_target_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "binary.py").write_bytes(b"\xff\xfe\xfd")
    (tmp_path / "test_app.py").write_text(
        "def test_ok() -> None:\n    assert True\n",
        encoding="utf-8",
    )
    spec = spec_for(
        tmp_path,
        mutable_paths=("binary.py",),
        context_paths=("test_app.py",),
    )
    baseline = run_baseline(spec, LocalPytestExecutor())

    with pytest.raises(UnsupportedMutationTargetError, match="UTF-8"):
        apply_mutation(
            spec,
            baseline,
            proposal(
                file_path="binary.py",
                original="x",
                mutated="y",
            ),
        )


def test_applied_mutation_evidence_is_serializable_and_stable(tmp_path: Path) -> None:
    write_workspace(tmp_path)
    spec, baseline = baseline_for(tmp_path)

    with apply_mutation(spec, baseline, proposal()) as first:
        first_payload = first.evidence.model_dump(mode="json")

    with apply_mutation(spec, baseline, proposal()) as second:
        second_payload = second.evidence.model_dump(mode="json")

    assert first_payload == second_payload
    assert first_payload["base_snapshot_id"] == baseline.source_snapshot_id
