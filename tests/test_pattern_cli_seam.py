"""CLI-seam tests for the pattern + scaffolding surface (#8).

Exercise the public CLI: a `pattern:` file through `caw graph`, and the
scaffolding commands `caw init` / `caw patterns list` / `caw patterns init`. The
scaffolded files are run end-to-end through `caw run` with the offline mock
Adapter (a real CLI-seam run, not just validate), so success is proven, not
asserted by inspecting internals (ADR 0008; project testing philosophy).
"""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from caw.cli import app

runner = CliRunner()


def _pipeline_pattern_file(directory: Path) -> Path:
    workflow_file = directory / "pipeline.yaml"
    workflow_file.write_text(
        "name: ci\n"
        "version: 1\n"
        "pattern:\n"
        "  type: pipeline\n"
        "  steps:\n"
        "    - id: build\n      kind: shell\n      inputs:\n        command: echo build\n"
        "    - id: test\n      kind: shell\n      inputs:\n        command: echo test\n"
        "    - id: deploy\n      kind: shell\n      inputs:\n        command: echo deploy\n",
        encoding="utf-8",
    )
    return workflow_file


def test_graph_shows_the_expanded_dag_of_a_pipeline_pattern(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # AC: a `pattern:` file's expanded DAG is visible in `caw graph` — the JSON
    # plan shows the plain nodes and the chained edges the expander produced, so a
    # user inspects the materialized graph before running it.
    workflow_file = _pipeline_pattern_file(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["graph", str(workflow_file), "--format", "json"])

    assert result.exit_code == 0, result.output
    plan = json.loads(result.output)
    assert [node["id"] for node in plan["nodes"]] == ["build", "test", "deploy"]
    assert plan["edges"] == [
        {"from": "build", "to": "test"},
        {"from": "test", "to": "deploy"},
    ], "the expander's chained edges show in the plan"
    assert plan["topological_order"] == ["build", "test", "deploy"]


def test_init_writes_a_starter_workflow_that_validates_and_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # AC: `caw init` creates a starter workflow that validates and runs. Write it,
    # then prove it through `caw validate` (exit 0) and an actual `caw run` (exit 0,
    # succeeded) — a real run, not just a validate.
    monkeypatch.chdir(tmp_path)

    init = runner.invoke(app, ["init"])
    assert init.exit_code == 0, init.output

    starter = tmp_path / "workflow.yaml"
    assert starter.is_file(), "init writes a starter workflow file by default"
    assert "workflow.yaml" in init.output, "init names the file it created"

    validated = runner.invoke(app, ["validate", str(starter)])
    assert validated.exit_code == 0, validated.output

    ran = runner.invoke(app, ["run", str(starter)])
    assert ran.exit_code == 0, ran.output
    assert "succeeded" in ran.output


def test_init_to_an_explicit_path_writes_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "nested" / "starter.yaml"
    target.parent.mkdir()

    result = runner.invoke(app, ["init", str(target)])

    assert result.exit_code == 0, result.output
    assert target.is_file(), "init writes to the explicit path"


def test_init_refuses_to_overwrite_an_existing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Scaffolding must never silently clobber an author's file: an existing target
    # is a config-class refusal (exit 2, one `error:` line), not an overwrite.
    monkeypatch.chdir(tmp_path)
    existing = tmp_path / "workflow.yaml"
    existing.write_text("name: mine\n", encoding="utf-8")

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 2
    assert result.exception is None or isinstance(result.exception, SystemExit)
    lines = [line for line in result.output.splitlines() if line.strip()]
    assert len(lines) == 1
    assert lines[0].startswith("error:")
    assert existing.read_text(encoding="utf-8") == "name: mine\n", "the file is untouched"
