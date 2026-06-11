"""CLI-seam tests: invoke the caw CLI and assert exit codes and stdout."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from caw.cli import app

runner = CliRunner()


def write_workflow(directory: Path, command: str) -> Path:
    workflow_file = directory / "workflow.yaml"
    workflow_file.write_text(
        "name: sample\n"
        "version: 1\n"
        "nodes:\n"
        "  - id: greet\n"
        "    kind: shell\n"
        "    inputs:\n"
        f"      command: {command!r}\n",
        encoding="utf-8",
    )
    return workflow_file


def test_help_exits_zero_and_names_the_cli() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "caw" in result.output


def test_run_succeeding_shell_node_exits_zero_and_reports_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow_file = write_workflow(tmp_path, "echo hello")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["run", str(workflow_file)])

    assert result.exit_code == 0
    assert "succeeded" in result.output
