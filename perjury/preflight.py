"""Explicit live-demo preflight (#20): every check reports under its own subsystem.

Subsystems: config, gemini, modal, baseline, workspace, evidence. A failed check blocks the checks
that depend on it (reported as ``blocked``, not as extra failures). The process exits 0 only when
every check passed; nothing is faked, cached or skipped silently. The full 6-10 mutation rehearsal is
a separate script (``scripts/closed_loop_smoke.py``), not hidden in here.

External boundaries (Gemini, Modal) are injected through ``Providers`` so the logic is testable
without credentials; the defaults perform the real calls.
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .contracts import ExecutionOutcome
from .workspace import WorkspaceExecutor

SUBSYSTEMS = ("config", "gemini", "modal", "baseline", "workspace", "evidence")
DEFAULT_MODEL = "google:gemini-3.8-flash"
ROOT = Path(__file__).resolve().parents[1]


class PreflightFailure(Exception):
    """A check found a real problem; the message is the operator-facing reason."""


@dataclass(frozen=True, slots=True)
class CheckResult:
    subsystem: str
    name: str
    status: str  # "pass" | "fail" | "blocked"
    detail: str
    duration_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.status == "pass"


@dataclass(slots=True)
class Providers:
    """The injectable external boundaries; defaults are live and resolved lazily."""

    gemini: Callable[[str], Any] | None = None
    modal_auth: Callable[[], Any] | None = None
    modal_canary: Callable[[], str] | None = None
    modal_executor: Callable[[], WorkspaceExecutor] | None = None
    local_executor: Callable[[], WorkspaceExecutor] | None = None


def _default_gemini(model: str) -> Any:
    from pydantic import BaseModel
    from pydantic_ai import Agent

    class Health(BaseModel):
        ok: bool
        message: str

    agent = Agent(
        model,
        output_type=Health,
        system_prompt="Return a concise structured health check.",
        defer_model_check=True,
    )
    return agent.run_sync("Confirm the PERJURY Gemini connection is working.").output


def _default_modal_auth() -> Any:
    from .modal_runner import _get_app

    return _get_app()


def _default_canary() -> str:
    import modal

    from .modal_runner import _get_app, _get_runtime

    app = _get_app()
    started = time.perf_counter()
    sandbox = modal.Sandbox.create(
        app=app,
        image=_get_runtime(),
        secrets=(),
        include_oidc_identity_token=False,
        timeout=120,
        block_network=True,
    )
    created = time.perf_counter()
    try:
        process = sandbox.exec("python", "-c", "print('perjury-canary-ok')", timeout=60)
        out, code = process.stdout.read(), process.wait()
    finally:
        sandbox.terminate(wait=True)
    finished = time.perf_counter()
    if code != 0 or "perjury-canary-ok" not in out:
        raise PreflightFailure(f"canary Sandbox ran but returned exit={code} output={out!r}")
    leaked = sum(1 for _ in modal.Sandbox.list(app_id=app.app_id))
    if leaked:
        raise PreflightFailure(
            f"{leaked} Sandbox(es) still running after the canary was terminated"
        )
    return (
        f"canary Sandbox started ({(created - started):.1f}s), executed and terminated "
        f"({(finished - started):.1f}s total); 0 leaked"
    )


def _default_modal_executor() -> WorkspaceExecutor:
    from .modal_runner import ModalWorkspaceExecutor

    return ModalWorkspaceExecutor()


def _default_local_executor() -> WorkspaceExecutor:
    from .workspace import LocalPytestExecutor

    return LocalPytestExecutor()


@dataclass(slots=True)
class Preflight:
    env: Mapping[str, str]
    root: Path = ROOT
    home: Path = field(default_factory=Path.home)
    providers: Providers = field(default_factory=Providers)
    results: list[CheckResult] = field(default_factory=list)
    baseline_cache: dict[str, Any] = field(default_factory=dict)

    # ----------------------------------------------------------------- plumbing
    def _status(self, key: str) -> str | None:
        for r in self.results:
            if f"{r.subsystem}.{r.name}" == key:
                return r.status
        return None

    def check(
        self,
        subsystem: str,
        name: str,
        fn: Callable[[], str],
        *,
        needs: Sequence[str] = (),
    ) -> CheckResult:
        """Run one check; never raises. Unmet dependencies yield ``blocked`` (not another failure)."""
        blockers = [n for n in needs if self._status(n) != "pass"]
        if blockers:
            result = CheckResult(
                subsystem, name, "blocked", f"blocked by failed check: {', '.join(blockers)}"
            )
        else:
            started = time.perf_counter()
            try:
                detail, status = fn(), "pass"
            except PreflightFailure as exc:
                detail, status = str(exc), "fail"
            except Exception as exc:  # noqa: BLE001 - a broken provider is a failure, not a crash
                detail, status = f"{type(exc).__name__}: {exc}", "fail"
            result = CheckResult(
                subsystem, name, status, detail, int((time.perf_counter() - started) * 1000)
            )
        self.results.append(result)
        return result

    # ----------------------------------------------------------------- config
    def _config(self) -> None:
        env = self.env

        def python_version() -> str:
            if sys.version_info[:2] != (3, 12):
                raise PreflightFailure(f"Python 3.12.x required, running {sys.version.split()[0]}")
            return f"Python {sys.version.split()[0]}"

        def api_key() -> str:
            if not env.get("GOOGLE_API_KEY", "").strip():
                raise PreflightFailure("GOOGLE_API_KEY is empty or missing (set it in .env)")
            return "GOOGLE_API_KEY is set"

        def model() -> str:
            value = env.get("PERJURY_MODEL", DEFAULT_MODEL).strip()
            if not value or ":" not in value:
                raise PreflightFailure(
                    f"PERJURY_MODEL must look like 'provider:model', got {value!r}"
                )
            return f"PERJURY_MODEL={value}"

        def mutation_count() -> str:
            from .planning import PlanningConfig

            raw = env.get("PERJURY_MUTATION_COUNT", "").strip()
            try:
                cfg = PlanningConfig(target_count=int(raw)) if raw else PlanningConfig()
            except ValueError as exc:
                raise PreflightFailure(
                    f"PERJURY_MUTATION_COUNT must be an integer 6-10 ({exc})"
                ) from exc
            return f"mutation target count {cfg.target_count} (contract 6-10)"

        def modal_credentials() -> str:
            if env.get("MODAL_TOKEN_ID") and env.get("MODAL_TOKEN_SECRET"):
                return "Modal token from environment"
            if (self.home / ".modal.toml").is_file():
                return "Modal profile found (~/.modal.toml)"
            raise PreflightFailure(
                "no Modal credentials: run `modal setup` (or set MODAL_TOKEN_ID/SECRET)"
            )

        self.check("config", "python", python_version)
        self.check("config", "google_api_key", api_key)
        self.check("config", "model", model)
        self.check("config", "mutation_count", mutation_count)
        self.check("config", "modal_credentials", modal_credentials)

    # ----------------------------------------------------------------- gemini
    def _gemini(self) -> None:
        model = self.env.get("PERJURY_MODEL", DEFAULT_MODEL).strip()

        def structured() -> str:
            call = self.providers.gemini or _default_gemini
            started = time.perf_counter()
            output = call(model)
            if not (hasattr(output, "ok") and hasattr(output, "message")):
                raise PreflightFailure(
                    f"model returned {type(output).__name__}, not the requested structured output"
                )
            return f"{model} returned a validated structured response in {time.perf_counter() - started:.1f}s"

        self.check(
            "gemini",
            "structured_response",
            structured,
            needs=["config.google_api_key", "config.model"],
        )

    # ----------------------------------------------------------------- modal
    def _modal(self) -> None:
        def auth() -> str:
            app = (self.providers.modal_auth or _default_modal_auth)()
            return f"Modal app resolved ({getattr(app, 'name', 'perjury')})"

        self.check("modal", "auth_and_app", auth, needs=["config.modal_credentials"])
        self.check(
            "modal",
            "canary_sandbox",
            lambda: (self.providers.modal_canary or _default_canary)(),
            needs=["modal.auth_and_app"],
        )

    # ----------------------------------------------------------------- baseline
    def _baseline(self) -> None:
        from .workspace import refund_workspace_spec, run_baseline

        spec = refund_workspace_spec(str(self.root))
        local_holder = self.baseline_cache

        def local() -> str:
            executor = (self.providers.local_executor or _default_local_executor)()
            baseline = run_baseline(spec, executor)  # raises BaselineNotReadyError unless PASS
            local_holder["baseline"] = baseline
            return (
                f"refund baseline PASS locally ({baseline.execution.duration_ms}ms, "
                f"manifest {baseline.manifest_sha256[:19]}…)"
            )

        def modal_parity() -> str:
            executor = (self.providers.modal_executor or _default_modal_executor)()
            baseline = run_baseline(spec, executor)
            expected = local_holder["baseline"].manifest_sha256
            if baseline.manifest_sha256 != expected:
                raise PreflightFailure("Modal and local baselines used different snapshot evidence")
            if baseline.execution.outcome is not ExecutionOutcome.PASS:
                raise PreflightFailure(f"Modal baseline outcome {baseline.execution.outcome.value}")
            return f"refund baseline PASS in Modal ({baseline.execution.duration_ms}ms), same manifest as local"

        self.check("baseline", "local_pass", local)
        self.check(
            "baseline",
            "modal_parity",
            modal_parity,
            needs=["baseline.local_pass", "modal.canary_sandbox"],
        )

    # ----------------------------------------------------------------- workspace
    def _workspace(self) -> None:
        def applicator() -> str:
            from .canonical import CanonicalRefundPlanner
            from .mutation import apply_mutation
            from .planning import plan_mutations
            from .workspace import refund_workspace_spec

            spec = refund_workspace_spec(str(self.root))
            baseline = self.baseline_cache["baseline"]
            plan = plan_mutations(spec, baseline, CanonicalRefundPlanner())
            m01 = plan.batch.mutations[0]
            with apply_mutation(spec, baseline, m01) as workspace:
                diff = workspace.evidence.diff
                root = workspace.root
                if "-    if days_since_purchase <= 30 or premium:" not in diff:
                    raise PreflightFailure("applicator diff for the known M01 path is wrong")
                if str(root) == spec.source_root or not root.exists():
                    raise PreflightFailure("applicator did not isolate the mutation workspace")
            if root.exists():
                raise PreflightFailure("applicator left its isolated workspace behind")
            return (
                f"applicator self-check ok: {len(plan.batch.mutations)} canonical mutations validated, "
                "M01 diff exact, isolation + cleanup verified"
            )

        self.check("workspace", "applicator_self_check", applicator, needs=["baseline.local_pass"])

    # ----------------------------------------------------------------- evidence
    def _evidence(self) -> None:
        def writable() -> str:
            from .evidence import REDACTED, sanitize, write_json_atomic

            runs = self.root / ".perjury" / "runs"
            probe = runs / f".preflight-{os.getpid()}"
            try:
                write_json_atomic(probe / "evidence.json", '{"probe": true}\n')
                if (probe / "evidence.json").read_text(encoding="utf-8") != '{"probe": true}\n':
                    raise PreflightFailure("evidence probe did not read back identically")
            except OSError as exc:
                raise PreflightFailure(f"cannot write {runs}: {exc}") from exc
            finally:
                shutil.rmtree(probe, ignore_errors=True)
            clean, report = sanitize({"stdout": "key AIza" + "x" * 35})
            if REDACTED not in clean["stdout"] or not report.redactions:
                raise PreflightFailure("redaction self-test failed")
            return f"{runs} is writable (atomic write verified); redaction self-test ok"

        self.check("evidence", "directory_writable", writable)

    # ----------------------------------------------------------------- entrypoint
    def run(self) -> list[CheckResult]:
        self._config()
        self._gemini()
        self._modal()
        self._baseline()
        self._workspace()
        self._evidence()
        return self.results

    @property
    def ok(self) -> bool:
        return bool(self.results) and all(r.ok for r in self.results)


def render(results: Sequence[CheckResult]) -> str:
    icon = {"pass": "PASS", "fail": "FAIL", "blocked": "BLOCKED"}
    lines = ["PERJURY preflight", ""]
    for subsystem in SUBSYSTEMS:
        group = [r for r in results if r.subsystem == subsystem]
        if not group:
            continue
        verdict = "ok" if all(r.ok for r in group) else "PROBLEM"
        lines.append(f"[{subsystem}] {verdict}")
        for r in group:
            lines.append(
                f"  {icon[r.status]:<7} {r.name:<22} {r.detail}"
                + (f"  ({r.duration_ms}ms)" if r.ok else "")
            )
    failed = [r for r in results if r.status == "fail"]
    lines.append("")
    if all(r.ok for r in results) and results:
        lines.append(
            "READY: every preflight check passed. (Run the rehearsal before claiming a timed demo.)"
        )
    else:
        groups = sorted({r.subsystem for r in failed}, key=SUBSYSTEMS.index) or ["(blocked checks)"]
        lines.append(f"NOT READY: failing subsystem(s): {', '.join(groups)}.")
        lines.append(
            "Do NOT claim the live demo is ready. See docs/DEMO_RUNBOOK.md, section 'Recovery'."
        )
    return "\n".join(lines)


def main(
    argv: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
    providers: Providers | None = None,
) -> int:
    from dotenv import dotenv_values

    del argv
    merged: dict[str, str] = {
        k: v for k, v in dotenv_values(ROOT / ".env").items() if v is not None
    }
    merged.update(os.environ if env is None else env)
    # The agents read the process environment, so mirror .env values the way the app does.
    for key, value in merged.items():
        if env is None:
            os.environ.setdefault(key, value)
    preflight = Preflight(env=merged, providers=providers or Providers())
    print(render(preflight.run()))
    return 0 if preflight.ok else 1
