"""CLI seam for the `caw loop` Run Group surface (#15, ADR 0009).

`caw loop` is a separate sub-typer (mirroring `caw patterns`), leaving
`caw run`/`caw resume`/`caw report` untouched — no group-vs-run detection. These
tests drive the public CLI end to end offline with the mock Adapter: a controller
spec runs a loop to done, resumes a group, and reports a group as one aggregate.
The exit-code contract mirrors the single-run CLI (0 done, 1 failed, 2 config).
"""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from caw.cli import app

runner = CliRunner()


def _group_id(output: str) -> str:
    """Extract the group id from `caw loop` output (the `group-...` token, colon stripped)."""
    for line in output.splitlines():
        for token in line.split():
            if token.startswith("group-"):
                return token.rstrip(":")
    raise AssertionError(f"no group id in output: {output!r}")


def _write_loop_bundle(directory: Path) -> Path:
    """Write a controller spec + iteration workflow + fixtures: a 2-iteration loop."""
    (directory / "iter1.fixture.json").write_text(
        json.dumps(
            {
                "exit_status": 0,
                "stdout": "CONTINUE",
                "structured_output": {"next_fixture": "iter2.fixture.json"},
            }
        ),
        encoding="utf-8",
    )
    (directory / "iter2.fixture.json").write_text(
        json.dumps({"exit_status": 0, "stdout": "FINISHED", "structured_output": {}}),
        encoding="utf-8",
    )
    (directory / "iteration.yaml").write_text(
        "name: loop-iteration\n"
        "version: 1\n"
        "nodes:\n"
        "  - id: verdict\n"
        "    kind: agent\n"
        "    inputs:\n"
        "      adapter: mock\n"
        "      prompt: Decide whether the task is done.\n"
        "      fixture: iter1.fixture.json\n",
        encoding="utf-8",
    )
    spec = directory / "loop.yaml"
    spec.write_text(
        "workflow: iteration.yaml\n"
        "max_iterations: 5\n"
        "evaluate_node: verdict\n"
        "done:\n"
        "  ref:\n"
        "    node: verdict\n"
        "    field: stdout\n"
        "  op: contains\n"
        "  value: FINISHED\n"
        "feedback:\n"
        "  to_node: verdict\n"
        "  to_field: fixture\n"
        "  from_field: next_fixture\n",
        encoding="utf-8",
    )
    return spec


def test_loop_run_drives_a_group_to_done_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # AC1/AC2/AC4/AC7: `caw loop run <spec>` drives the loop to done offline,
    # exit 0, naming the group and its stop reason.
    spec = _write_loop_bundle(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["loop", "run", str(spec)])

    assert result.exit_code == 0, result.output
    assert "done" in result.output
    assert "group-" in result.output, "the group id is named in the output"
    # The group materialized under .caw/groups with two iteration run dirs.
    groups = list((tmp_path / ".caw" / "groups").iterdir())
    assert len(groups) == 1
    iterations = list((groups[0] / "iterations").iterdir())
    assert len(iterations) == 2


def test_loop_report_aggregates_the_group(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # AC6: `caw loop report <group_id>` renders one aggregate over all iterations.
    spec = _write_loop_bundle(tmp_path)
    monkeypatch.chdir(tmp_path)

    run_result = runner.invoke(app, ["loop", "run", str(spec)])
    assert run_result.exit_code == 0, run_result.output
    group_id = _group_id(run_result.output)

    report_result = runner.invoke(app, ["loop", "report", group_id])
    assert report_result.exit_code == 0, report_result.output
    report = json.loads(report_result.output)
    assert report["group_id"] == group_id
    assert report["status"] == "done"
    assert len(report["iterations"]) == 2


def test_loop_run_with_a_failing_iteration_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The exit contract mirrors `caw run`: a failed constituent Run exits 1.
    (tmp_path / "fail.fixture.json").write_text(
        json.dumps({"exit_status": 1, "stderr": "boom"}), encoding="utf-8"
    )
    (tmp_path / "iteration.yaml").write_text(
        "name: loop-iteration\nversion: 1\nnodes:\n"
        "  - id: verdict\n    kind: agent\n    inputs:\n"
        "      adapter: mock\n      prompt: go\n      fixture: fail.fixture.json\n",
        encoding="utf-8",
    )
    (tmp_path / "loop.yaml").write_text(
        "workflow: iteration.yaml\nmax_iterations: 3\nevaluate_node: verdict\n"
        "done:\n  ref:\n    node: verdict\n    field: stdout\n  op: contains\n  value: FINISHED\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["loop", "run", str(tmp_path / "loop.yaml")])

    assert result.exit_code == 1, result.output
    assert "failed" in result.output


def test_loop_run_with_an_invalid_spec_is_a_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A bad spec is a config-class refusal: exit 2, one `error:` line, no run dir.
    bad = tmp_path / "loop.yaml"
    bad.write_text("max_iterations: 3\n", encoding="utf-8")  # missing required fields
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["loop", "run", str(bad)])

    assert result.exit_code == 2
    lines = [line for line in result.output.splitlines() if line.strip()]
    assert lines[0].startswith("error:")


def test_loop_report_unknown_group_is_a_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["loop", "report", "group-nope"])

    assert result.exit_code == 2
    lines = [line for line in result.output.splitlines() if line.strip()]
    assert lines[0].startswith("error:")
    assert "group-nope" in lines[0]


def test_loop_init_scaffolds_a_runnable_loop_until_done_example(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # AC7: loop-until-done ships at least one example. `caw loop init` scaffolds a
    # COMPLETE, runnable bundle (spec + iteration workflow + fixtures); running it
    # drives the loop to done offline with the mock Adapter — exit 0, a real run.
    monkeypatch.chdir(tmp_path)

    scaffolded = runner.invoke(app, ["loop", "init"])
    assert scaffolded.exit_code == 0, scaffolded.output

    spec = tmp_path / "loop.yaml"
    assert spec.is_file(), "loop init writes the controller spec by default"

    ran = runner.invoke(app, ["loop", "run", str(spec)])
    assert ran.exit_code == 0, ran.output
    assert "done" in ran.output, "the scaffolded loop reaches done"


def test_loop_init_refuses_to_overwrite_an_existing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    existing = tmp_path / "loop.yaml"
    existing.write_text("workflow: mine\n", encoding="utf-8")

    result = runner.invoke(app, ["loop", "init"])

    assert result.exit_code == 2
    assert existing.read_text(encoding="utf-8") == "workflow: mine\n", "the file is untouched"


def test_loop_resume_continues_a_group(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # AC5: `caw loop resume <group_id>` resumes a group. Cap the first pass to one
    # iteration (stops exhausted), bump the cap, and resume to completion.
    spec = _write_loop_bundle(tmp_path)
    spec.write_text(spec.read_text().replace("max_iterations: 5", "max_iterations: 1"))
    monkeypatch.chdir(tmp_path)

    first = runner.invoke(app, ["loop", "run", str(spec)])
    assert first.exit_code == 0, first.output
    assert "exhausted" in first.output
    group_id = _group_id(first.output)

    # Interruption shape: a real interruption between iterations persists the
    # in-progress `running` marker (not the terminal `exhausted`). Mark it `running`
    # and reopen the cap so the resume continues past iteration 1.
    state_path = tmp_path / ".caw" / "groups" / group_id / "group.json"
    persisted = json.loads(state_path.read_text())
    persisted["spec"]["max_iterations"] = 5
    persisted["status"] = "running"
    state_path.write_text(json.dumps(persisted, indent=2) + "\n")

    resumed = runner.invoke(app, ["loop", "resume", group_id])
    assert resumed.exit_code == 0, resumed.output
    assert "done" in resumed.output
    report = json.loads(runner.invoke(app, ["loop", "report", group_id]).output)
    assert len(report["iterations"]) == 2, "the resume ran iteration 2 without re-running 1"
