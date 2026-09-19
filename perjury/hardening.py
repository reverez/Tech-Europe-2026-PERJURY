"""Deterministic two-world verification of a generated test (#14).

Both worlds are built from the SAME baseline snapshot and contain the byte-identical candidate:

    original world = baseline snapshot + candidate
    mutant world   = baseline snapshot + selected mutation + candidate

The same structured pytest command (explicitly naming the candidate) runs in both through the
existing bounded fan-out and executor. The semantic verdict is the existing
``judge_verification``: verified only for original PASS + mutant TEST_FAIL. INVALID, TIMEOUT and
INFRA_ERROR can never verify. Identity mismatches raise ``WorldConsistencyError`` before any
execution, so inconsistent evidence cannot produce a verdict.
"""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from pydantic import BaseModel

from .candidate import CandidatePlan, spec_with_candidate, write_candidate
from .contracts import (
    AppliedMutation,
    BaselineResult,
    ExecutionResult,
    HardeningResult,
    MutationProposal,
    SnapshotManifest,
    VerificationEvidence,
    WorkspaceSpec,
)
from .execution import FanoutRunner
from .fanout import FanoutJob, run_fanout
from .mutation import apply_mutation, materialize_baseline_workspace
from .verification import judge_verification
from .workspace import WorkspaceError, WorkspaceExecutor, build_snapshot_manifest


class WorldConsistencyError(WorkspaceError):
    """The two worlds (or their inputs) are not comparable; nothing may be verified."""


@dataclass(slots=True)
class VerificationWorld:
    kind: str
    root: Path
    spec: WorkspaceSpec
    manifest: SnapshotManifest


@dataclass(slots=True)
class VerificationWorlds:
    original: VerificationWorld
    mutant: VerificationWorld
    applied: AppliedMutation
    _cleaned: bool = False

    def cleanup(self) -> None:
        if not self._cleaned:
            shutil.rmtree(self.original.root, ignore_errors=True)
            shutil.rmtree(self.mutant.root, ignore_errors=True)
            self._cleaned = True

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.cleanup()


def _files(manifest: SnapshotManifest) -> dict[str, str]:
    return {f.path: f.sha256 for f in manifest.files}


def _check_inputs(
    baseline: BaselineResult, mutation: MutationProposal, candidate: CandidatePlan
) -> None:
    if candidate.mutation_id != mutation.id:
        raise WorldConsistencyError(
            f"candidate is for {candidate.mutation_id!r} but the mutation is {mutation.id!r}"
        )
    if (
        candidate.base_manifest_sha256 != baseline.manifest_sha256
        or candidate.base_snapshot_id != baseline.source_snapshot_id
    ):
        raise WorldConsistencyError("candidate was planned against a different baseline snapshot")
    if hashlib.sha256(candidate.code.encode("utf-8")).hexdigest() != candidate.sha256:
        raise WorldConsistencyError("candidate code does not match its recorded hash")


def _assert_worlds_consistent(
    baseline: BaselineResult,
    candidate: CandidatePlan,
    applied: AppliedMutation,
    original: VerificationWorld,
    mutant: VerificationWorld,
) -> None:
    base = _files(baseline.manifest)
    if candidate.candidate_path in base:
        raise WorldConsistencyError("candidate path already exists in the baseline snapshot")

    expected_original = {**base, candidate.candidate_path: candidate.sha256}
    expected_mutant = {
        **base,
        applied.target_path: applied.mutated_sha256,
        candidate.candidate_path: candidate.sha256,
    }
    if _files(original.manifest) != expected_original:
        raise WorldConsistencyError("original world differs from baseline + candidate")
    if _files(mutant.manifest) != expected_mutant:
        raise WorldConsistencyError("mutant world differs from baseline + mutation + candidate")
    if applied.base_manifest_sha256 != baseline.manifest_sha256:
        raise WorldConsistencyError("mutation was applied to a different baseline")
    if base[applied.target_path] != applied.original_sha256:
        raise WorldConsistencyError("mutation original hash does not match the baseline file")
    if original.spec.pytest_argv != mutant.spec.pytest_argv:
        raise WorldConsistencyError("worlds use different pytest commands")
    if candidate.candidate_path not in original.spec.pytest_argv:
        raise WorldConsistencyError("pytest command does not explicitly include the candidate")
    for world in (original, mutant):
        written = (world.root / candidate.candidate_path).read_bytes()
        if hashlib.sha256(written).hexdigest() != candidate.sha256:
            raise WorldConsistencyError(f"{world.kind} world candidate bytes differ from the plan")


def build_verification_worlds(
    spec: WorkspaceSpec,
    baseline: BaselineResult,
    mutation: MutationProposal,
    candidate: CandidatePlan,
) -> VerificationWorlds:
    """Build both isolated worlds from the baseline; caller owns cleanup (context manager)."""
    _check_inputs(baseline, mutation, candidate)
    tag = f"{mutation.id}:{candidate.sha256[:16]}"

    original_root = materialize_baseline_workspace(spec, baseline)
    mutant_workspace = None
    try:
        write_candidate(original_root, candidate)
        original_spec = spec_with_candidate(
            spec, original_root, candidate, snapshot_id=f"verify-original:{tag}"
        )
        original = VerificationWorld(
            "original", original_root, original_spec, build_snapshot_manifest(original_spec)
        )

        mutant_workspace = apply_mutation(spec, baseline, mutation)
        write_candidate(mutant_workspace.root, candidate)
        # Identical to the original world's spec except for the source root and snapshot id.
        mutant_spec = original_spec.model_copy(
            update={
                "source_root": str(mutant_workspace.root),
                "snapshot_id": f"verify-mutant:{tag}",
            }
        )
        mutant = VerificationWorld(
            "mutant", mutant_workspace.root, mutant_spec, build_snapshot_manifest(mutant_spec)
        )
        _assert_worlds_consistent(baseline, candidate, mutant_workspace.evidence, original, mutant)
        return VerificationWorlds(original, mutant, mutant_workspace.evidence)
    except BaseException:
        shutil.rmtree(original_root, ignore_errors=True)
        if mutant_workspace is not None:
            mutant_workspace.cleanup()
        raise


class HardeningVerification(BaseModel):
    """Verdict plus the retained audit trail for both worlds."""

    result: HardeningResult
    original: ExecutionResult
    mutant: ExecutionResult
    candidate_path: str
    candidate_sha256: str
    candidate_diff: str


def build_evidence(
    baseline: BaselineResult,
    candidate: CandidatePlan,
    applied: AppliedMutation,
    worlds_manifests: tuple[SnapshotManifest, SnapshotManifest],
    pytest_argv: tuple[str, ...],
    original: ExecutionResult,
    mutant: ExecutionResult,
) -> VerificationEvidence:
    return VerificationEvidence(
        mutation_id=candidate.mutation_id,
        original_outcome=original.outcome,
        mutant_outcome=mutant.outcome,
        original_exit_code=original.exit_code,
        mutant_exit_code=mutant.exit_code,
        original_stdout=original.stdout,
        mutant_stdout=mutant.stdout,
        original_duration_ms=original.duration_ms,
        mutant_duration_ms=mutant.duration_ms,
        base_manifest_sha256=baseline.manifest_sha256,
        candidate_path=candidate.candidate_path,
        candidate_sha256=candidate.sha256,
        original_manifest_sha256=worlds_manifests[0].manifest_sha256,
        mutant_manifest_sha256=worlds_manifests[1].manifest_sha256,
        mutated_sha256=applied.mutated_sha256,
        pytest_argv=pytest_argv,
    )


def verify_candidate(
    spec: WorkspaceSpec,
    baseline: BaselineResult,
    mutation: MutationProposal,
    candidate: CandidatePlan,
    executor: WorkspaceExecutor,
    *,
    fanout_runner: FanoutRunner = run_fanout,
) -> HardeningVerification:
    """Run both worlds and return the deterministic verdict with audit evidence."""
    mid = mutation.id
    with build_verification_worlds(spec, baseline, mutation, candidate) as worlds:
        jobs = [
            FanoutJob(f"original:{mid}", worlds.original.spec, worlds.original.manifest),
            FanoutJob(f"mutant:{mid}", worlds.mutant.spec, worlds.mutant.manifest),
        ]
        fanned = fanout_runner(jobs, executor, max_concurrency=2)
        by_id = {r.mutation_id: r for r in fanned.results}
        if set(by_id) != {j.mutation_id for j in jobs} or len(fanned.results) != 2:
            raise WorldConsistencyError("fan-out did not return exactly one result per world")
        original = by_id[f"original:{mid}"].execution
        mutant = by_id[f"mutant:{mid}"].execution
        evidence = build_evidence(
            baseline,
            candidate,
            worlds.applied,
            (worlds.original.manifest, worlds.mutant.manifest),
            worlds.original.spec.pytest_argv,
            original,
            mutant,
        )
    return HardeningVerification(
        result=judge_verification(evidence, mutation_id=mid),
        original=original,
        mutant=mutant,
        candidate_path=candidate.candidate_path,
        candidate_sha256=candidate.sha256,
        candidate_diff=candidate.diff,
    )
