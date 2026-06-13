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


class TestReleaseWorkflowLayout:
    """The release lifecycle lives in a single release.yml (issue #51).

    Merging release-please.yml and release-build.yml is safe once the raw
    ``push: tags`` build trigger is gone: with no GITHUB_TOKEN-created tag to
    dodge, the ``workflow_call`` indirection is unnecessary and the build runs
    as a downstream job in the same run.
    """

    def test_single_release_file_exists(self) -> None:
        names = {path.name for path in workflow_files()}
        assert "release.yml" in names, "the release lifecycle must live in release.yml"

    def test_old_split_release_files_are_gone(self) -> None:
        names = {path.name for path in workflow_files()}
        assert "release-please.yml" not in names, "release-please.yml must be merged away"
        assert "release-build.yml" not in names, "release-build.yml must be merged away"


class TestReleaseTriggers:
    """release.yml triggers on push to main and on a manual workflow_dispatch rebuild."""

    def test_triggers_on_push_to_main(self) -> None:
        triggers = triggers_of(load_workflow("release.yml"))
        assert triggers["push"]["branches"] == ["main"]

    def test_has_no_raw_tag_push_build_trigger(self) -> None:
        # Plan B removes the raw `push: tags: v*` trigger so a human-chosen tag
        # name can no longer ship artifacts of an unreconciled pyproject version.
        triggers = triggers_of(load_workflow("release.yml"))
        assert "tags" not in triggers.get("push", {}), (
            "raw `push: tags` build trigger must be removed (single version authority)"
        )

    def test_workflow_dispatch_takes_an_existing_tag(self) -> None:
        # The manual escape hatch re-builds an existing release; the tag input is
        # required because dispatch never computes a version of its own.
        triggers = triggers_of(load_workflow("release.yml"))
        assert "workflow_dispatch" in triggers, "manual rebuild path must be workflow_dispatch"
        tag_input = triggers["workflow_dispatch"]["inputs"]["tag"]
        assert tag_input["required"] is True
        assert tag_input["type"] == "string"


class TestReleasePleaseJob:
    """Conventional commits on main maintain a Release PR via the release-please action."""

    def test_release_please_runs_only_on_push(self) -> None:
        # On workflow_dispatch there is no version to compute, so the action is
        # skipped and the build runs against the dispatched tag instead.
        job = load_workflow("release.yml")["jobs"]["release-please"]
        assert "github.event_name == 'push'" in job["if"]

    def test_uses_the_release_please_action(self) -> None:
        job = load_workflow("release.yml")["jobs"]["release-please"]
        uses = [step["uses"] for step in job.get("steps", []) if "uses" in step]
        assert any(entry.startswith("googleapis/release-please-action@") for entry in uses)

    def test_release_please_exposes_the_four_outputs(self) -> None:
        outputs = load_workflow("release.yml")["jobs"]["release-please"].get("outputs", {})
        for name in ("release_created", "tag_name", "prs_created", "pr"):
            assert name in outputs, f"release-please must expose the {name!r} output"


class TestReleaseBuildJob:
    """The build is the back half of a release: a downstream job, never a re-trigger."""

    def _build_job(self) -> dict[str, Any]:
        job = load_workflow("release.yml")["jobs"]["build"]
        assert isinstance(job, dict)
        return job

    def test_build_needs_release_please(self) -> None:
        needs = self._build_job()["needs"]
        needs = [needs] if isinstance(needs, str) else needs
        assert "release-please" in needs, "build must run after release-please in the same run"

    def test_build_gates_on_both_release_created_and_dispatch(self) -> None:
        # Dual origin: (a) the automatic push path when a release was created, and
        # (b) workflow_dispatch. `always()` keeps the build reachable even though
        # release-please is skipped on dispatch.
        condition = self._build_job()["if"]
        assert "always()" in condition, "build must stay reachable when release-please is skipped"
        assert "release_created == 'true'" in condition, "automatic path: gate on release_created"
        assert "github.event_name == 'push'" in condition, "automatic path is the push event"
        assert "github.event_name == 'workflow_dispatch'" in condition, "manual rebuild path"

    def test_build_tag_comes_from_dispatch_input_or_release_output(self) -> None:
        # The tag is the dispatched input on manual rebuilds, else release-please's
        # computed tag on the automatic path; never a human-typed name on push.
        dumped = yaml.safe_dump(self._build_job())
        assert "github.event.inputs.tag" in dumped, "dispatch path must use the input tag"
        assert "needs.release-please.outputs.tag_name" in dumped, "push path uses the release tag"

    def test_build_checks_out_the_tag_and_builds_with_uv(self) -> None:
        job = self._build_job()
        checkout = next(
            s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/checkout@")
        )
        assert "with" in checkout and "ref" in checkout["with"], "build must checkout the tag ref"
        commands = [step["run"] for step in job["steps"] if "run" in step]
        assert any("uv build" in command for command in commands)

    def test_release_creation_and_upload_are_idempotent(self) -> None:
        joined = "\n".join(step["run"] for step in self._build_job()["steps"] if "run" in step)
        assert "gh release view" in joined, "must check for an existing Release first"
        assert "gh release create" in joined, "must create the Release if it is missing"
        assert "gh release upload" in joined
        assert "--clobber" in joined, "uploads must be idempotent to guard double-runs"

    def test_build_keeps_least_privilege_write_permission(self) -> None:
        assert self._build_job().get("permissions") == {"contents": "write"}

    def test_build_is_serialized_per_tag(self) -> None:
        # A concurrency group keyed on the tag prevents an automatic build and a
        # manual rebuild of the same release from racing each other's uploads.
        group = yaml.safe_dump(self._build_job().get("concurrency", {}))
        assert "tag" in group, "build concurrency must be keyed on the tag"


class TestReleasePleaseLockfileSync:
    """A Release PR must carry a uv.lock that matches its bumped pyproject (issue #46).

    release-please bumps pyproject.toml and src/caw/__init__.py but never touches
    uv.lock, which embeds the project's own ``caw`` version. Without re-locking on
    the Release PR branch, the PR's ``uv sync --locked`` gate fails (Release PR #45).
    """

    def _sync_job(self) -> dict[str, Any]:
        jobs = load_workflow("release.yml")["jobs"]
        sync_jobs = [
            job
            for name, job in jobs.items()
            if name not in ("release-please", "build")
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
        # Dispatch never touches a Release PR, so the re-lock is push-only.
        assert "github.event_name == 'push'" in job["if"]

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
        release_job = load_workflow("release.yml")["jobs"]["release-please"]
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
