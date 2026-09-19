from __future__ import annotations

import difflib
import hashlib
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Self

from .contracts import (
    AppliedMutation,
    BaselineResult,
    MutationProposal,
    SnapshotFile,
    SnapshotManifest,
    WorkspaceSpec,
)
from .workspace import SnapshotPolicyError, build_snapshot_manifest


class MutationApplicationError(RuntimeError):
    """Base error for deterministic mutation application."""


class UnsafeMutationPathError(MutationApplicationError):
    """Raised when a proposal targets a path outside the mutable allowlist."""


class StaleSnapshotError(MutationApplicationError):
    """Raised when source bytes no longer match the baseline manifest."""


class MutationAnchorError(MutationApplicationError):
    """Raised when the requested exact replacement anchor is unusable."""


class UnsupportedMutationTargetError(MutationApplicationError):
    """Raised when the target is not supported text source for the MVP."""


@dataclass(slots=True)
class MutationWorkspace:
    root: Path
    spec: WorkspaceSpec
    manifest: SnapshotManifest
    evidence: AppliedMutation
    _cleaned: bool = False

    def cleanup(self) -> None:
        if not self._cleaned:
            shutil.rmtree(self.root, ignore_errors=True)
            self._cleaned = True

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.cleanup()


def _normalize_target_path(value: str) -> str:
    if not value or "\x00" in value or "\\" in value:
        raise UnsafeMutationPathError(
            "Mutation file_path must be a non-empty POSIX repository-relative path."
        )
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or str(path) == ".":
        raise UnsafeMutationPathError(f"Unsafe mutation file_path: {value!r}")
    return str(path)


def _is_within(candidate: str, allowlist: tuple[str, ...]) -> bool:
    path = PurePosixPath(candidate)
    return any(
        path == PurePosixPath(allowed) or PurePosixPath(allowed) in path.parents
        for allowed in allowlist
    )


def _manifest_entry(manifest: SnapshotManifest, target_path: str) -> SnapshotFile:
    for entry in manifest.files:
        if entry.path == target_path:
            return entry
    raise UnsupportedMutationTargetError(
        f"Mutation target is absent from the sanitized baseline manifest: {target_path!r}"
    )


def _safe_source_file(root: Path, relative_path: str) -> Path:
    current = root
    for part in PurePosixPath(relative_path).parts:
        current = current / part
        if current.is_symlink():
            raise StaleSnapshotError(
                f"Source path became a symlink after baseline: {relative_path!r}"
            )

    try:
        resolved = current.resolve(strict=True)
        resolved.relative_to(root)
    except (FileNotFoundError, ValueError) as exc:
        raise StaleSnapshotError(
            f"Source path no longer resolves inside the baseline root: {relative_path!r}"
        ) from exc

    if not resolved.is_file():
        raise StaleSnapshotError(
            f"Baseline file is no longer a regular file: {relative_path!r}"
        )
    return resolved


def _read_verified_file(root: Path, entry: SnapshotFile) -> bytes:
    path = _safe_source_file(root, entry.path)
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != entry.sha256:
        raise StaleSnapshotError(
            f"Source file hash drifted since baseline for {entry.path!r}: "
            f"expected {entry.sha256}, got {digest}"
        )
    return data


def _assert_baseline_identity(
    spec: WorkspaceSpec,
    baseline: BaselineResult,
) -> None:
    if not baseline.ready_for_mutation:
        raise StaleSnapshotError("Baseline evidence is not mutation-ready.")
    if baseline.workspace_id != spec.workspace_id:
        raise StaleSnapshotError("WorkspaceSpec workspace_id does not match baseline evidence.")
    if baseline.source_snapshot_id != spec.snapshot_id:
        raise StaleSnapshotError("WorkspaceSpec snapshot_id does not match baseline evidence.")
    if baseline.manifest.manifest_sha256 != baseline.manifest_sha256:
        raise StaleSnapshotError("Baseline manifest evidence is internally inconsistent.")

    try:
        current = build_snapshot_manifest(spec)
    except SnapshotPolicyError as exc:
        raise StaleSnapshotError(
            f"Source no longer satisfies the baseline snapshot policy: {exc}"
        ) from exc

    if current.manifest_sha256 != baseline.manifest_sha256:
        raise StaleSnapshotError(
            "Source snapshot drifted after baseline: "
            f"expected {baseline.manifest_sha256}, got {current.manifest_sha256}"
        )


def _materialize_exact_manifest(
    spec: WorkspaceSpec,
    manifest: SnapshotManifest,
    destination: Path,
) -> None:
    root = Path(spec.source_root).expanduser().resolve()
    for entry in manifest.files:
        data = _read_verified_file(root, entry)
        target = destination / PurePosixPath(entry.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def _unified_diff(target_path: str, original: str, mutated: str) -> str:
    return "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            mutated.splitlines(keepends=True),
            fromfile=f"a/{target_path}",
            tofile=f"b/{target_path}",
            lineterm="\n",
        )
    )


def apply_mutation(
    spec: WorkspaceSpec,
    baseline: BaselineResult,
    proposal: MutationProposal,
) -> MutationWorkspace:
    """Apply one exact semantic mutation to an isolated baseline snapshot copy."""
    target_path = _normalize_target_path(proposal.file_path)
    if not _is_within(target_path, spec.mutable_paths):
        raise UnsafeMutationPathError(
            f"Mutation target is outside mutable implementation paths: {target_path!r}"
        )
    if _is_within(target_path, spec.context_paths):
        raise UnsafeMutationPathError(
            f"Mutation target is context/test-only and cannot be edited: {target_path!r}"
        )

    if proposal.original_snippet == proposal.mutated_snippet:
        raise MutationAnchorError("Mutation replacement is a no-op.")

    _assert_baseline_identity(spec, baseline)
    entry = _manifest_entry(baseline.manifest, target_path)
    root = Path(spec.source_root).expanduser().resolve()
    target_bytes = _read_verified_file(root, entry)
    try:
        original_text = target_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UnsupportedMutationTargetError(
            f"Mutation target is not valid UTF-8 text: {target_path!r}"
        ) from exc

    matches = original_text.count(proposal.original_snippet)
    if matches != 1:
        raise MutationAnchorError(
            f"original_snippet must match exactly once in {target_path!r}; found {matches}."
        )

    mutated_text = original_text.replace(
        proposal.original_snippet,
        proposal.mutated_snippet,
        1,
    )
    mutated_bytes = mutated_text.encode("utf-8")
    original_sha256 = hashlib.sha256(target_bytes).hexdigest()
    mutated_sha256 = hashlib.sha256(mutated_bytes).hexdigest()
    if original_sha256 == mutated_sha256:
        raise MutationAnchorError("Mutation did not change target file bytes.")

    diff = _unified_diff(target_path, original_text, mutated_text)
    if not diff:
        raise MutationAnchorError("Mutation produced no unified diff.")

    isolated_root = Path(
        tempfile.mkdtemp(prefix=f"perjury-{spec.workspace_id}-{proposal.id}-")
    ).resolve()
    try:
        _materialize_exact_manifest(spec, baseline.manifest, isolated_root)
        isolated_target = isolated_root / PurePosixPath(target_path)
        isolated_target.write_bytes(mutated_bytes)

        snapshot_seed = hashlib.sha256(
            (
                baseline.manifest_sha256
                + "\0"
                + proposal.id
                + "\0"
                + target_path
                + "\0"
                + mutated_sha256
            ).encode("utf-8")
        ).hexdigest()
        mutated_spec = spec.model_copy(
            update={
                "source_root": str(isolated_root),
                "snapshot_id": f"mutation:{proposal.id}:{snapshot_seed[:32]}",
            }
        )
        mutated_manifest = build_snapshot_manifest(mutated_spec)

        evidence = AppliedMutation(
            mutation_id=proposal.id,
            base_snapshot_id=baseline.source_snapshot_id,
            base_manifest_sha256=baseline.manifest_sha256,
            target_path=target_path,
            original_sha256=original_sha256,
            mutated_sha256=mutated_sha256,
            mutated_manifest_sha256=mutated_manifest.manifest_sha256,
            diff=diff,
        )
        return MutationWorkspace(
            root=isolated_root,
            spec=mutated_spec,
            manifest=mutated_manifest,
            evidence=evidence,
        )
    except Exception:
        shutil.rmtree(isolated_root, ignore_errors=True)
        raise
