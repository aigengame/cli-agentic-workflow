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


def test_run_failing_node_prints_its_stderr_excerpt(
    write_workflow: Callable[[str], Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow_file = write_workflow("echo diagnostic detail >&2; exit 7")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["run", str(workflow_file)])

    assert result.exit_code == 1
    assert "diagnostic detail" in result.output


def test_run_failing_node_stderr_excerpt_is_bounded_to_the_last_lines(
    write_workflow: Callable[[str], Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow_file = write_workflow(
        'for i in $(seq 1 100); do echo "stderr line $i" >&2; done; exit 7'
    )
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["run", str(workflow_file)])

    assert result.exit_code == 1
    assert "stderr line 100" in result.output
    assert "stderr line 81" in result.output, "the last 20 lines are shown"
    assert "stderr line 80\n" not in result.output, "earlier lines are cut"


def test_run_infra_failure_exits_three_with_one_error_line_and_no_traceback(
    write_workflow: Callable[[str], Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow_file = write_workflow("echo hello")
    caw_dir = tmp_path / ".caw"
    caw_dir.mkdir()
    caw_dir.chmod(0o555)
    monkeypatch.chdir(tmp_path)

    try:
        result = runner.invoke(app, ["run", str(workflow_file)])
    finally:
        caw_dir.chmod(0o755)

    assert result.exit_code == 3, "infra errors are distinct from success, node failure, config"
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "error:" in result.output
    assert "Traceback" not in result.output


def test_run_missing_workflow_file_fails_with_an_error_naming_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["run", str(tmp_path / "absent.yaml")])

    assert result.exit_code == 2
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "absent.yaml" in result.output


def test_run_rejects_duplicate_node_ids_before_executing_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "marker.txt"
    workflow_file = tmp_path / "workflow.yaml"
    workflow_file.write_text(
        "name: sample\n"
        "version: 1\n"
        "nodes:\n"
        "  - id: greet\n"
        "    kind: shell\n"
        "    inputs:\n"
        f"      command: touch {marker}\n"
        "  - id: greet\n"
        "    kind: shell\n"
        "    inputs:\n"
        f"      command: touch {marker}\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["run", str(workflow_file)])

    assert result.exit_code == 2
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert not marker.exists(), "no node executes for a duplicate-id workflow"
    assert not (tmp_path / ".caw").exists()


def test_run_rejects_an_empty_nodes_list_instead_of_vacuously_succeeding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow_file = tmp_path / "workflow.yaml"
    workflow_file.write_text("name: sample\nversion: 1\nnodes: []\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["run", str(workflow_file)])

    assert result.exit_code == 2
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert not (tmp_path / ".caw").exists()


@pytest.mark.parametrize(
    ("field", "yaml_text"),
    [
        (
            "name",
            'name: "  "\nversion: 1\nnodes:\n'
            "  - id: greet\n    kind: shell\n    inputs:\n      command: echo hello\n",
        ),
        (
            "node id",
            "name: sample\nversion: 1\nnodes:\n"
            '  - id: "  "\n    kind: shell\n    inputs:\n      command: echo hello\n',
        ),
        (
            "command",
            "name: sample\nversion: 1\nnodes:\n"
            '  - id: greet\n    kind: shell\n    inputs:\n      command: "  "\n',
        ),
    ],
)
def test_run_rejects_blank_or_whitespace_only_fields(
    field: str, yaml_text: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow_file = tmp_path / "workflow.yaml"
    workflow_file.write_text(yaml_text, encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["run", str(workflow_file)])

    assert result.exit_code == 2, f"blank {field} must be a config error"
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert not (tmp_path / ".caw").exists()


def test_run_rejects_duplicate_yaml_mapping_keys_instead_of_dropping_half_the_workflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow_file = tmp_path / "workflow.yaml"
    workflow_file.write_text(
        "name: sample\n"
        "version: 1\n"
        "nodes:\n"
        "  - id: first\n    kind: shell\n    inputs:\n      command: echo first\n"
        "nodes:\n"
        "  - id: second\n    kind: shell\n    inputs:\n      command: echo second\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["run", str(workflow_file)])

    assert result.exit_code == 2
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert not (tmp_path / ".caw").exists()


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
    assert "invalid.yaml" in result.output
    assert not (tmp_path / ".caw").exists(), "no run directory is created for invalid input"
