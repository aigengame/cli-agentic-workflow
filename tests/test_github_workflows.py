"""Contract tests for the GitHub Actions CI and release automation (issue #33).

These tests parse .github/workflows/*.yml and pin the automation contract:
triggers, the Python matrix, the four quality gates, action pinning, and
permissions hygiene. They are deliberately coupled to the workflow *contract*
(what runs, when) rather than to step ordering or cosmetic details.
"""

import json
import re
import tomllib
from pathlib import Path
from typing import Any

import yaml

import caw

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

QUALITY_GATES = (
    "uv run ruff check .",
    "uv run ruff format --check .",
    "uv run mypy",
    "uv run pytest",
)


def load_workflow(filename: str) -> dict[Any, Any]:
    """Parse one workflow file into a mapping.

    Keys are ``Any`` rather than ``str`` because YAML 1.1 parses the bare
    workflow key ``on`` as boolean ``True``.
    """
    workflow = yaml.safe_load((WORKFLOWS_DIR / filename).read_text(encoding="utf-8"))
    assert isinstance(workflow, dict), f"{filename} did not parse to a mapping"
    return workflow


def triggers_of(workflow: dict[Any, Any]) -> dict[str, Any]:
    """Return the workflow's trigger mapping.

    YAML 1.1 parses the bare key ``on`` as boolean ``True``, so the trigger
    mapping may be stored under either key depending on quoting.
    """
    triggers = workflow.get("on", workflow.get(True))
    assert isinstance(triggers, dict), "workflow has no trigger mapping"
    return triggers


def run_commands_of(workflow: dict[Any, Any]) -> list[str]:
    """Collect every ``run:`` command string across all jobs."""
    return [
        step["run"]
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if "run" in step
    ]


def workflow_files() -> list[Path]:
    return sorted(WORKFLOWS_DIR.glob("*.yml"))


class TestCiWorkflow:
    """CI runs the four quality gates on PRs and pushes to main, on 3.12 + 3.13."""

    def test_triggers_on_pull_request_and_push_to_main(self) -> None:
        triggers = triggers_of(load_workflow("ci.yml"))
        assert "pull_request" in triggers
        assert triggers["push"]["branches"] == ["main"]

    def test_matrix_covers_python_312_and_313(self) -> None:
        (job,) = load_workflow("ci.yml")["jobs"].values()
        assert job["strategy"]["matrix"]["python-version"] == ["3.12", "3.13"]

    def test_installs_dependencies_from_the_lockfile(self) -> None:
        commands = run_commands_of(load_workflow("ci.yml"))
        assert any("uv sync --locked" in command for command in commands)

    def test_runs_the_four_quality_gates_via_uv(self) -> None:
        commands = run_commands_of(load_workflow("ci.yml"))
        for gate in QUALITY_GATES:
            assert any(gate in command for command in commands), f"missing gate: {gate}"


class TestReleaseBuildWorkflow:
    """Both release trigger paths converge on one tag-driven build workflow."""

    def test_triggers_on_version_tags_and_workflow_call(self) -> None:
        triggers = triggers_of(load_workflow("release-build.yml"))
        assert triggers["push"]["tags"] == ["v*"]
        assert "workflow_call" in triggers

    def test_builds_distributions_with_uv(self) -> None:
        commands = run_commands_of(load_workflow("release-build.yml"))
        assert any("uv build" in command for command in commands)

    def test_release_creation_and_upload_are_idempotent(self) -> None:
        joined = "\n".join(run_commands_of(load_workflow("release-build.yml")))
        assert "gh release view" in joined, "must check for an existing Release first"
        assert "gh release create" in joined, "must create the Release for manual tags"
        assert "gh release upload" in joined
        assert "--clobber" in joined, "uploads must be idempotent to guard double-runs"


class TestReleasePleaseWorkflow:
    """Conventional commits on main maintain a Release PR; merging hands off to the build."""

    def test_runs_on_push_to_main(self) -> None:
        triggers = triggers_of(load_workflow("release-please.yml"))
        assert triggers["push"]["branches"] == ["main"]

    def test_uses_the_release_please_action(self) -> None:
        workflow = load_workflow("release-please.yml")
        uses = [
            step["uses"]
            for job in workflow["jobs"].values()
            for step in job.get("steps", [])
            if "uses" in step
        ]
        assert any(entry.startswith("googleapis/release-please-action@") for entry in uses)

    def test_release_creation_hands_off_to_release_build(self) -> None:
        build_job = load_workflow("release-please.yml")["jobs"]["build"]
        assert build_job["uses"] == "./.github/workflows/release-build.yml"
        assert "release_created" in build_job["if"]


class TestReleasePleaseLockfileSync:
    """A Release PR must carry a uv.lock that matches its bumped pyproject (issue #46).

    release-please bumps pyproject.toml and src/caw/__init__.py but never touches
    uv.lock, which embeds the project's own ``caw`` version. Without re-locking on
    the Release PR branch, the PR's ``uv sync --locked`` gate fails (Release PR #45).
    """

    def _sync_job(self) -> dict[str, Any]:
        jobs = load_workflow("release-please.yml")["jobs"]
        sync_jobs = [
            job
            for name, job in jobs.items()
            if name != "release-please"
            and any("uv lock" in step.get("run", "") for step in job.get("steps", []))
        ]
        assert len(sync_jobs) == 1, "expected exactly one job that runs `uv lock`"
        job = sync_jobs[0]
        assert isinstance(job, dict)
        return job

    def test_sync_job_is_guarded_on_a_release_pr_being_touched(self) -> None:
        job = self._sync_job()
        # prs_created is true when a Release PR is created OR updated, so the
        # re-lock fires on the version-bump PR rather than only at release time.
        assert "needs.release-please.outputs.prs_created" in job["if"]

    def test_sync_job_checks_out_the_release_pr_branch(self) -> None:
        job = self._sync_job()
        # The release branch comes from the action's `pr` output (PullRequest JSON,
        # field headBranchName); checking out main would re-lock the wrong branch.
        assert "headBranchName" in yaml.safe_dump(job)
        assert "needs.release-please.outputs.pr" in yaml.safe_dump(job)

    def test_sync_job_relocks_and_pushes_uv_lock(self) -> None:
        joined = "\n".join(step.get("run", "") for step in self._sync_job().get("steps", []))
        assert "uv lock" in joined, "must refresh the lockfile"
        assert "uv.lock" in joined, "must act on the lockfile specifically"
        assert "git push" in joined, "must push the refreshed lockfile back"

    def test_sync_job_avoids_empty_commits(self) -> None:
        joined = "\n".join(step.get("run", "") for step in self._sync_job().get("steps", []))
        # No-op when the lockfile is already in sync: a diff check must gate the
        # commit so an unchanged uv.lock does not produce an empty commit.
        assert "git diff" in joined, "must guard the commit on an actual change"

    def test_sync_job_keeps_least_privilege_write_permission(self) -> None:
        job = self._sync_job()
        assert job.get("permissions") == {"contents": "write"}

    def test_release_please_exposes_the_pr_output(self) -> None:
        release_job = load_workflow("release-please.yml")["jobs"]["release-please"]
        outputs = release_job.get("outputs", {})
        assert "pr" in outputs, "downstream re-lock needs the Release PR JSON"
        assert "prs_created" in outputs, "downstream re-lock needs the touched guard"


class TestReleasePleaseConfig:
    """The release-please config bumps the Python project version from conventional commits."""

    def test_config_parses_and_uses_the_python_release_type(self) -> None:
        config = json.loads((REPO_ROOT / "release-please-config.json").read_text(encoding="utf-8"))
        package = config["packages"]["."]
        assert package["release-type"] == "python"
        assert package["include-component-in-tag"] is False, "tags must be plain v*"

    def test_manifest_matches_pyproject_and_package_versions(self) -> None:
        manifest = json.loads(
            (REPO_ROOT / ".release-please-manifest.json").read_text(encoding="utf-8")
        )
        pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        assert manifest["."] == pyproject["project"]["version"]
        assert caw.__version__ == pyproject["project"]["version"]


class TestWorkflowHygiene:
    """Cross-cutting pins: actions locked to commit SHAs, permissions declared."""

    def test_workflow_files_exist(self) -> None:
        assert workflow_files(), f"no workflow files under {WORKFLOWS_DIR}"

    def test_actions_are_pinned_to_full_commit_shas(self) -> None:
        for path in workflow_files():
            workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
            for job in workflow["jobs"].values():
                steps: list[dict[str, Any]] = job.get("steps", [])
                for unit in [job, *steps]:
                    uses = unit.get("uses")
                    if uses is None or uses.startswith("./"):
                        continue
                    _, _, ref = uses.partition("@")
                    assert re.fullmatch(r"[0-9a-f]{40}", ref), (
                        f"{path.name}: {uses!r} is not pinned to a full commit SHA"
                    )

    def test_every_workflow_declares_permissions(self) -> None:
        for path in workflow_files():
            workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
            assert "permissions" in workflow, f"{path.name} has no permissions block"
