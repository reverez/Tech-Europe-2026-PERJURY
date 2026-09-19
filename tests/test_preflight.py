"""Preflight (#20): subsystem-grouped, non-fabricated, exit code 0 only when everything passes."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from perjury.contracts import ExecutionOutcome as O
from perjury.contracts import ExecutionResult
from perjury.preflight import SUBSYSTEMS, Preflight, Providers, main, render
from perjury.workspace import LocalPytestExecutor

ROOT = Path(__file__).resolve().parents[1]
GOOD_ENV = {
    "GOOGLE_API_KEY": "test-key-not-real",
    "PERJURY_MODEL": "google:gemini-3.8-flash",
    "PERJURY_MUTATION_COUNT": "8",
    "MODAL_TOKEN_ID": "ak-test",
    "MODAL_TOKEN_SECRET": "as-test",
}


class ModalLikeLocalExecutor:
    """Stands in for ModalWorkspaceExecutor by running the same contract locally."""

    def __init__(self, outcome: O | None = None) -> None:
        self.outcome, self.real = outcome, LocalPytestExecutor()

    def execute(self, spec, *, execution_id, manifest=None):
        if self.outcome is not None:
            return ExecutionResult(
                execution_id=execution_id, outcome=self.outcome, duration_ms=1,
                exit_code=1 if self.outcome is O.TEST_FAIL else None,
            )  # fmt: skip
        return self.real.execute(spec, execution_id=execution_id, manifest=manifest)


def good_providers(**overrides) -> Providers:
    values = {
        "gemini": lambda model: SimpleNamespace(ok=True, message="healthy"),
        "modal_auth": lambda: SimpleNamespace(name="perjury"),
        "modal_canary": lambda: "canary ok",
        "modal_executor": lambda: ModalLikeLocalExecutor(),
        "local_executor": lambda: LocalPytestExecutor(),
    }
    values.update(overrides)
    return Providers(**values)


def run_preflight(tmp_path: Path, env=None, root: Path | None = None, **providers) -> Preflight:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    pf = Preflight(
        env=GOOD_ENV if env is None else env,
        root=root or tmp_path / "repo",
        home=home,
        providers=good_providers(**providers),
    )
    if root is None:  # a scratch repo copy is not needed: only .perjury is written under root
        (tmp_path / "repo").mkdir(exist_ok=True)
        pf.root = ROOT  # baseline/applicator need the real fixture...
    pf.run()
    return pf


def by_key(pf: Preflight) -> dict[str, str]:
    return {f"{r.subsystem}.{r.name}": r.status for r in pf.results}


@pytest.fixture(autouse=True)
def isolated_evidence_dir(tmp_path, monkeypatch):
    """Point the evidence probe at tmp without changing the fixture root used for baselines."""
    import perjury.preflight as module

    real_run = module.Preflight._evidence

    def evidence(self):
        original = self.root
        self.root = tmp_path / "evidence-root"
        self.root.mkdir(exist_ok=True)
        try:
            real_run(self)
        finally:
            self.root = original

    monkeypatch.setattr(module.Preflight, "_evidence", evidence)


# ------------------------------------------------------------------ all good


def test_all_checks_pass_with_healthy_boundaries(tmp_path) -> None:
    pf = run_preflight(tmp_path)
    assert pf.ok, render(pf.results)
    assert {r.subsystem for r in pf.results} == set(SUBSYSTEMS)
    assert all(r.status == "pass" for r in pf.results)
    text = render(pf.results)
    assert text.count("READY: every preflight check passed") == 1 and "NOT READY" not in text
    for subsystem in SUBSYSTEMS:
        assert f"[{subsystem}] ok" in text


def test_required_checks_are_present() -> None:
    pf = Preflight(env=GOOD_ENV, providers=good_providers())
    keys = {"config.python", "config.google_api_key", "config.model", "config.mutation_count",
            "config.modal_credentials", "gemini.structured_response", "modal.auth_and_app",
            "modal.canary_sandbox", "baseline.local_pass", "baseline.modal_parity",
            "workspace.applicator_self_check", "evidence.directory_writable"}  # fmt: skip
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        pf.home = Path(tmp)
        assert keys <= set(by_key(_run(pf)))


def _run(pf: Preflight) -> Preflight:
    pf.run()
    return pf


# ------------------------------------------------------------------ config failures


@pytest.mark.parametrize(
    "env_change,check",
    [
        ({"GOOGLE_API_KEY": ""}, "config.google_api_key"),
        ({"GOOGLE_API_KEY": "   "}, "config.google_api_key"),
        ({"PERJURY_MODEL": "gemini"}, "config.model"),
        ({"PERJURY_MUTATION_COUNT": "3"}, "config.mutation_count"),
        ({"PERJURY_MUTATION_COUNT": "abc"}, "config.mutation_count"),
        ({"PERJURY_MUTATION_COUNT": "11"}, "config.mutation_count"),
    ],
)
def test_config_problems_are_reported_under_config(tmp_path, env_change, check) -> None:
    pf = run_preflight(tmp_path, env={**GOOD_ENV, **env_change})
    assert not pf.ok
    assert by_key(pf)[check] == "fail"
    assert "[config] PROBLEM" in render(pf.results)


def test_missing_modal_credentials_block_modal_checks_only(tmp_path) -> None:
    env = {k: v for k, v in GOOD_ENV.items() if not k.startswith("MODAL_")}
    pf = run_preflight(tmp_path, env=env)
    status = by_key(pf)
    assert status["config.modal_credentials"] == "fail"
    assert status["modal.auth_and_app"] == status["modal.canary_sandbox"] == "blocked"
    assert status["baseline.modal_parity"] == "blocked"
    assert (
        status["baseline.local_pass"] == "pass" and status["gemini.structured_response"] == "pass"
    )


def test_modal_profile_file_counts_as_credentials(tmp_path) -> None:
    env = {k: v for k, v in GOOD_ENV.items() if not k.startswith("MODAL_")}
    (tmp_path / "home").mkdir()
    (tmp_path / "home" / ".modal.toml").write_text("[default]\n")
    pf = run_preflight(tmp_path, env=env)
    assert by_key(pf)["config.modal_credentials"] == "pass"


def test_missing_api_key_blocks_gemini_without_calling_it(tmp_path) -> None:
    calls = []
    pf = run_preflight(
        tmp_path, env={**GOOD_ENV, "GOOGLE_API_KEY": ""}, gemini=lambda m: calls.append(m)
    )
    assert calls == [] and by_key(pf)["gemini.structured_response"] == "blocked"
    assert "blocked by failed check: config.google_api_key" in render(pf.results)


# ------------------------------------------------------------------ gemini


def test_gemini_must_return_structured_output(tmp_path) -> None:
    pf = run_preflight(tmp_path, gemini=lambda model: "free text, not structured")
    assert by_key(pf)["gemini.structured_response"] == "fail"
    assert "not the requested structured output" in render(pf.results)


def test_gemini_provider_error_is_grouped_not_raised(tmp_path) -> None:
    def boom(model):
        raise RuntimeError("quota exceeded")

    pf = run_preflight(tmp_path, gemini=boom)
    text = render(pf.results)
    assert by_key(pf)["gemini.structured_response"] == "fail"
    assert "[gemini] PROBLEM" in text and "quota exceeded" in text
    assert by_key(pf)["baseline.local_pass"] == "pass"  # other subsystems still checked


def test_gemini_receives_the_configured_model(tmp_path) -> None:
    seen = []
    run_preflight(
        tmp_path, env={**GOOD_ENV, "PERJURY_MODEL": "google:custom"},
        gemini=lambda m: (seen.append(m), SimpleNamespace(ok=True, message="x"))[1],
    )  # fmt: skip
    assert seen == ["google:custom"]


# ------------------------------------------------------------------ modal


def test_modal_auth_failure_blocks_the_canary(tmp_path) -> None:
    def denied():
        raise RuntimeError("token expired")

    pf = run_preflight(tmp_path, modal_auth=denied)
    status = by_key(pf)
    assert status["modal.auth_and_app"] == "fail" and status["modal.canary_sandbox"] == "blocked"
    assert status["baseline.modal_parity"] == "blocked"
    assert "token expired" in render(pf.results)


def test_canary_failure_is_a_modal_failure(tmp_path) -> None:
    def failing():
        from perjury.preflight import PreflightFailure

        raise PreflightFailure("1 Sandbox(es) still running after the canary was terminated")

    pf = run_preflight(tmp_path, modal_canary=failing)
    assert by_key(pf)["modal.canary_sandbox"] == "fail"
    assert "still running" in render(pf.results)


# ------------------------------------------------------------------ baseline / workspace / evidence


def test_red_baseline_fails_baseline_and_blocks_the_applicator_check(tmp_path) -> None:
    pf = run_preflight(tmp_path, local_executor=lambda: ModalLikeLocalExecutor(O.TEST_FAIL))
    status = by_key(pf)
    assert status["baseline.local_pass"] == "fail"
    assert status["workspace.applicator_self_check"] == "blocked"
    assert status["baseline.modal_parity"] == "blocked"
    assert "[baseline] PROBLEM" in render(pf.results)


def test_modal_baseline_that_does_not_pass_fails_baseline(tmp_path) -> None:
    pf = run_preflight(tmp_path, modal_executor=lambda: ModalLikeLocalExecutor(O.INFRA_ERROR))
    assert by_key(pf)["baseline.modal_parity"] == "fail"


def test_applicator_self_check_runs_for_real(tmp_path) -> None:
    pf = run_preflight(tmp_path)
    detail = next(r for r in pf.results if r.name == "applicator_self_check").detail
    assert "M01 diff exact" in detail and "cleanup verified" in detail


def test_unwritable_evidence_directory_is_an_evidence_failure(tmp_path, monkeypatch) -> None:
    import perjury.preflight as module

    def evidence(self):  # root is a *file*, so .perjury/runs cannot be created
        blocker = tmp_path / "blocker"
        blocker.write_text("x")
        self.root = blocker
        self.check("evidence", "directory_writable", lambda: _raise_via_real(self))

    def _raise_via_real(self):
        from perjury.evidence import write_json_atomic
        from perjury.preflight import PreflightFailure

        try:
            write_json_atomic(self.root / ".perjury" / "runs" / "p" / "evidence.json", "{}")
        except OSError as exc:
            raise PreflightFailure(f"cannot write evidence dir: {exc}") from exc
        return "unexpectedly writable"

    monkeypatch.setattr(module.Preflight, "_evidence", evidence)
    pf = Preflight(env=GOOD_ENV, root=ROOT, home=tmp_path, providers=good_providers())
    pf.run()
    assert by_key(pf)["evidence.directory_writable"] == "fail"
    assert "[evidence] PROBLEM" in render(pf.results)


# ------------------------------------------------------------------ exit code / honesty


def test_exit_code_is_zero_only_when_everything_passes(capsys) -> None:
    assert main(env=GOOD_ENV, providers=good_providers()) == 0
    assert "READY" in capsys.readouterr().out
    assert main(env={**GOOD_ENV, "GOOGLE_API_KEY": ""}, providers=good_providers()) == 1
    out = capsys.readouterr().out
    assert "NOT READY" in out and "config" in out and "Do NOT claim the live demo is ready" in out


def test_a_failed_preflight_never_prints_ready(tmp_path) -> None:
    pf = run_preflight(tmp_path, gemini=lambda m: None)
    assert "READY: every" not in render(pf.results) and not pf.ok


def test_no_results_is_not_success() -> None:
    assert Preflight(env={}, providers=good_providers()).ok is False


def test_every_pass_is_backed_by_a_provider_call_not_a_constant(tmp_path) -> None:
    calls: list[str] = []
    providers = {
        "gemini": lambda m: (calls.append("gemini"), SimpleNamespace(ok=True, message="x"))[1],
        "modal_auth": lambda: (calls.append("auth"), SimpleNamespace(name="p"))[1],
        "modal_canary": lambda: (calls.append("canary"), "ok")[1],
        "modal_executor": lambda: (calls.append("modal_exec"), ModalLikeLocalExecutor())[1],
        "local_executor": lambda: (calls.append("local_exec"), LocalPytestExecutor())[1],
    }
    pf = run_preflight(tmp_path, **providers)
    assert pf.ok and calls == ["gemini", "auth", "canary", "local_exec", "modal_exec"]


def test_script_entrypoint_exists_and_uses_main() -> None:
    text = (ROOT / "scripts" / "preflight.py").read_text()
    assert "from perjury.preflight import main" in text and "sys.exit(main())" in text
