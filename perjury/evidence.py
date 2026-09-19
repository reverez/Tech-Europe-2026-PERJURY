"""Sanitized, atomic run evidence bundle (#19).

The bundle embeds the existing orchestration contracts (``RunResult`` and everything it retains:
stage timings/evidence refs, correlated events, plan + mutation diffs, per-mutation semantic
outcomes with bounded output and truncation flags, survivor analysis, candidate, verification and
the authoritative #25 comparison) instead of defining a second schema. It adds only provenance,
identity (run/commit), redaction/truncation reports and an integrity hash.

Policy
- The process environment is never serialized; only environment *variable names* of the workspace
  are listed. Secret-looking keys/values are replaced by ``[REDACTED]`` and reported.
- Oversized strings are truncated with a marker and reported, so evidence never implies completeness.
- ``complete`` is false whenever anything was redacted or truncated.
- Files are written atomically (temp file + fsync + rename) under ``.perjury/runs/{run_id}/``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .contracts import WorkspaceSpec
from .orchestrator import RunEvent, RunResult

SCHEMA_VERSION = "perjury.evidence.v1"
REDACTED = "[REDACTED]"
MAX_STRING_CHARS = 65_536
MAX_REPORT_ENTRIES = 200
RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

_SECRET_KEY = re.compile(
    r"(?i)(api[_-]?key|secret|token|passw(or)?d|private[_-]?key|credential|authorization|cookie)"
)
_SECRET_VALUE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)"
    r"|AIza[0-9A-Za-z_\-]{35}"
    r"|\bAKIA[0-9A-Z]{16}\b"
    r"|\bsk-[A-Za-z0-9_\-]{20,}"
    r"|\bgh[pousr]_[A-Za-z0-9]{36,}"
    r"|\bxox[abprs]-[A-Za-z0-9\-]{10,}"
    r"|(?i:\bbearer)\s+[A-Za-z0-9._~+/=\-]{16,}"
    r"|(?i:(?:api[_-]?key|token|secret|passw(?:or)?d)\s*[:=]\s*['\"]?)[^\s'\",;]{8,}"
)

# JSON pointers (into the bundle) grouped by what kind of statement they are.
PROVENANCE: dict[str, list[str]] = {
    "model_proposal": [
        "run.plan.batch",
        "run.plan.rejected",
        "run.selection.analyses",
        "run.candidate.plan.code",
        "run.candidate.plan.test_name",
    ],
    "executed_fact": [
        "run.baseline_outcome",
        "run.first_pass.trials",
        "run.candidate.collection",
        "run.verification.original",
        "run.verification.mutant",
        "run.rescore.after_report.trials",
        "run.stages",
    ],
    "derived_metric": [
        "run.first_pass.score",
        "run.rescore.comparison",
        "run.verification.result",
        "run.total_ms",
    ],
}


@dataclass(slots=True)
class SanitizeReport:
    redactions: list[dict[str, str]] = field(default_factory=list)
    truncations: list[dict[str, Any]] = field(default_factory=list)
    dropped_report_entries: int = 0

    def redact(self, path: str, kind: str) -> None:
        if len(self.redactions) < MAX_REPORT_ENTRIES:
            self.redactions.append({"path": path, "kind": kind})
        else:
            self.dropped_report_entries += 1

    def truncate(self, path: str, original_chars: int) -> None:
        if len(self.truncations) < MAX_REPORT_ENTRIES:
            self.truncations.append({"path": path, "original_chars": original_chars})
        else:
            self.dropped_report_entries += 1


def secret_values_from_env(env: Mapping[str, str] | None = None) -> list[str]:
    """Values of secret-looking environment variables, used only to scrub strings (never stored)."""
    source = os.environ if env is None else env
    return sorted(
        {v for k, v in source.items() if _SECRET_KEY.search(k) and len(v) >= 8},
        key=len,
        reverse=True,
    )


def _scrub_string(value: str, path: str, secrets: list[str], report: SanitizeReport) -> str:
    for secret in secrets:
        if secret in value:
            value = value.replace(secret, REDACTED)
            report.redact(path, "environment_secret")
    scrubbed, count = _SECRET_VALUE.subn(REDACTED, value)
    if count:
        report.redact(path, "secret_pattern")
        value = scrubbed
    if len(value) > MAX_STRING_CHARS:
        report.truncate(path, len(value))
        value = value[:MAX_STRING_CHARS] + f"…[truncated {len(value) - MAX_STRING_CHARS} chars]"
    return value


def sanitize(
    value: Any,
    secrets: list[str] | None = None,
    report: SanitizeReport | None = None,
    path: str = "",
) -> tuple[Any, SanitizeReport]:
    """Return a redacted, bounded copy of JSON-like ``value`` plus a report of what changed."""
    secrets = secrets or []
    report = report or SanitizeReport()
    if isinstance(value, str):
        return _scrub_string(value, path, secrets, report), report
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            child = f"{path}.{key}" if path else str(key)
            if _SECRET_KEY.search(str(key)) and isinstance(item, str | int | float) and item != "":
                out[str(key)] = REDACTED
                report.redact(child, "secret_key")
            else:
                out[str(key)] = sanitize(item, secrets, report, child)[0]
        return out, report
    if isinstance(value, list | tuple):
        return [
            sanitize(v, secrets, report, f"{path}[{i}]")[0] for i, v in enumerate(value)
        ], report
    return value, report


def _spec_view(spec: WorkspaceSpec) -> dict[str, Any]:
    """Sanitized WorkspaceSpec: no host path, and environment *names* only (never values)."""
    data = spec.model_dump(mode="json")
    data["source_root"] = Path(spec.source_root).name or "<workspace>"
    data["environment_keys"] = sorted(data.pop("environment", {}))
    return data


def canonical_json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def build_evidence_bundle(
    *,
    run_id: str,
    spec: WorkspaceSpec,
    commit_sha: str | None,
    result: RunResult | None,
    events: list[RunEvent] | None = None,
    crash: tuple[str, str] | None = None,
    env: Mapping[str, str] | None = None,
    written_at_ms: int | None = None,
) -> dict[str, Any]:
    """Assemble, sanitize and hash the evidence bundle for one run (finished, failed or crashed)."""
    if not RUN_ID.fullmatch(run_id):
        raise ValueError(f"unsafe run_id: {run_id!r}")
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "commit_sha": commit_sha,
        "written_at_ms": written_at_ms if written_at_ms is not None else int(time.time() * 1000),
        "workspace": _spec_view(spec),
        "outcome": (
            {
                "state": result.state.value,
                "reason": result.reason.value,
                "message": result.message,
                "total_ms": result.total_ms,
                "improvement_verified": result.improvement_verified,
            }
            if result is not None
            else {
                "state": "failed",
                "reason": crash[0] if crash else "unknown",
                "message": crash[1] if crash else "",
            }
        ),
        "provenance": PROVENANCE,
        "run": result.model_dump(mode="json") if result is not None else None,
        "crash": {"code": crash[0], "message": crash[1]} if crash else None,
        "events": None if result is not None else [e.model_dump(mode="json") for e in events or []],
    }
    clean, report = sanitize(payload, secret_values_from_env(env))
    clean["redactions"] = report.redactions
    clean["truncations"] = report.truncations
    clean["complete"] = not (
        report.redactions or report.truncations or report.dropped_report_entries
    )
    clean["bundle_sha256"] = "sha256:" + hashlib.sha256(canonical_json(clean).encode()).hexdigest()
    return clean


def verify_bundle_hash(bundle: Mapping[str, Any]) -> bool:
    body = {k: v for k, v in bundle.items() if k != "bundle_sha256"}
    expected = "sha256:" + hashlib.sha256(canonical_json(body).encode()).hexdigest()
    return bundle.get("bundle_sha256") == expected


def write_json_atomic(path: Path, text: str) -> None:
    """Write ``text`` so readers see the old file or the complete new one, never a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    run_id: str
    path: str  # repository-relative, e.g. .perjury/runs/<id>/evidence.json
    bundle_sha256: str
    bytes: int
    complete: bool


class EvidenceStore:
    """Writes/reads ``{root}/runs/{run_id}/evidence.json`` for the demo workspace."""

    def __init__(
        self,
        root: Path,
        spec: WorkspaceSpec,
        commit_sha: str | Callable[[], str | None] | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.root = Path(root)
        self.spec = spec
        self._commit = commit_sha  # a callable is resolved lazily, at save time (no import effects)
        self._env = env

    @property
    def commit_sha(self) -> str | None:
        return self._commit() if callable(self._commit) else self._commit

    def path_for(self, run_id: str) -> Path:
        if not RUN_ID.fullmatch(run_id):
            raise ValueError(f"unsafe run_id: {run_id!r}")
        return self.root / "runs" / run_id / "evidence.json"

    def save(
        self,
        run_id: str,
        result: RunResult | None,
        events: list[RunEvent] | None = None,
        crash: tuple[str, str] | None = None,
    ) -> EvidenceRef:
        bundle = build_evidence_bundle(
            run_id=run_id,
            spec=self.spec,
            commit_sha=self.commit_sha,
            result=result,
            events=events,
            crash=crash,
            env=self._env,
        )
        text = canonical_json(bundle)
        path = self.path_for(run_id)
        write_json_atomic(path, text)
        return EvidenceRef(
            run_id=run_id,
            path=f".perjury/runs/{run_id}/evidence.json",
            bundle_sha256=bundle["bundle_sha256"],
            bytes=len(text.encode("utf-8")),
            complete=bundle["complete"],
        )

    def load(self, run_id: str) -> dict[str, Any] | None:
        try:
            path = self.path_for(run_id)
        except ValueError:
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return None
