"""CLI-seam tests: invoke the caw CLI and assert exit codes and stdout."""

from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner

from caw.cli import app

runner = CliRunner()


def test_help_exits_zero_and_names_the_cli() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "caw" in result.output


def test_run_succeeding_shell_node_exits_zero_and_reports_success(
    write_workflow: Callable[[str], Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow_file = write_workflow("echo hello")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["run", str(workflow_file)])

    assert result.exit_code == 0
    assert "succeeded" in result.output


def test_run_failing_shell_node_exits_nonzero_and_reports_failure(
    write_workflow: Callable[[str], Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow_file = write_workflow("exit 7")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["run", str(workflow_file)])

    assert result.exit_code != 0
    assert "failed" in result.output
    assert "exited 7" in result.output


def test_run_missing_workflow_file_fails_with_an_error_naming_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["run", str(tmp_path / "absent.yaml")])

    assert result.exit_code == 2
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "absent.yaml" in result.output + result.stderr


def test_run_invalid_workflow_definition_fails_before_executing_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow_file = tmp_path / "invalid.yaml"
    workflow_file.write_text(
        "name: broken\nversion: 1\nnodes:\n  - id: greet\n    kind: rocket\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["run", str(workflow_file)])

    assert result.exit_code == 2
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "invalid.yaml" in result.output + result.stderr
    assert not (tmp_path / ".caw").exists(), "no run directory is created for invalid input"
