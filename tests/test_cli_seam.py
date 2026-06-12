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


def test_run_accepts_yaml_merge_keys_in_a_workflow_definition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow_file = tmp_path / "workflow.yaml"
    workflow_file.write_text(
        "name: sample\n"
        "version: 1\n"
        "nodes:\n"
        "  - id: first\n"
        "    kind: shell\n"
        "    inputs: &base\n"
        "      command: echo hello\n"
        "  - id: second\n"
        "    kind: shell\n"
        "    inputs:\n"
        "      <<: *base\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["run", str(workflow_file)])

    assert result.exit_code == 0, result.output
    assert "succeeded" in result.output


def test_run_allows_an_explicit_key_to_override_a_merged_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "overridden.txt"
    workflow_file = tmp_path / "workflow.yaml"
    workflow_file.write_text(
        "name: sample\n"
        "version: 1\n"
        "nodes:\n"
        "  - id: first\n"
        "    kind: shell\n"
        "    inputs: &base\n"
        "      command: echo from-base\n"
        "  - id: second\n"
        "    kind: shell\n"
        "    inputs:\n"
        "      <<: *base\n"
        f"      command: touch {marker}\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["run", str(workflow_file)])

    # YAML merge semantics: an explicit key legally overrides a merged one and
    # must not be rejected as a duplicate; the marker proves the override ran.
    assert result.exit_code == 0, result.output
    assert marker.exists()


def test_run_rejects_an_unhashable_yaml_mapping_key_as_a_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow_file = tmp_path / "workflow.yaml"
    workflow_file.write_text("? [a, b]\n: c\nname: sample\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["run", str(workflow_file)])

    assert result.exit_code == 2
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert not (tmp_path / ".caw").exists()


@pytest.mark.parametrize(
    ("case", "yaml_text"),
    [
        (
            "validation error",
            "name: sample\nversion: 1\nnodes:\n"
            "  - id: greet\n    kind: shell\n    inputs:\n      command: echo hello\n"
            "  - id: greet\n    kind: shell\n    inputs:\n      command: echo hello\n",
        ),
        (
            "yaml error",
            "name: sample\nname: again\nversion: 1\nnodes:\n"
            "  - id: greet\n    kind: shell\n    inputs:\n      command: echo hello\n",
        ),
    ],
)
def test_run_config_errors_print_a_single_error_line(
    case: str, yaml_text: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow_file = tmp_path / "workflow.yaml"
    workflow_file.write_text(yaml_text, encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["run", str(workflow_file)])

    assert result.exit_code == 2
    lines = [line for line in result.output.splitlines() if line.strip()]
    assert len(lines) == 1, f"a {case} must print a single error line, got:\n{result.output}"
    assert lines[0].startswith("error:")


def test_run_rejects_a_needs_reference_to_a_nonexistent_node_naming_file_and_node(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow_file = tmp_path / "workflow.yaml"
    workflow_file.write_text(
        "name: sample\n"
        "version: 1\n"
        "nodes:\n"
        "  - id: deploy\n"
        "    kind: shell\n"
        "    needs: [build]\n"
        "    inputs:\n"
        "      command: echo deploy\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["run", str(workflow_file)])

    assert result.exit_code == 2
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "workflow.yaml" in result.output, "the error names the workflow file"
    assert "deploy" in result.output, "the error names the referencing node"
    assert "build" in result.output, "the error names the unknown reference"
    assert not (tmp_path / ".caw").exists()


def test_run_rejects_a_node_that_needs_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow_file = tmp_path / "workflow.yaml"
    workflow_file.write_text(
        "name: sample\n"
        "version: 1\n"
        "nodes:\n"
        "  - id: build\n"
        "    kind: shell\n"
        "    needs: [build]\n"
        "    inputs:\n"
        "      command: echo build\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["run", str(workflow_file)])

    assert result.exit_code == 2
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "workflow.yaml" in result.output
    assert "build" in result.output, "the error names the self-referencing node"
    assert not (tmp_path / ".caw").exists()


def test_run_rejects_a_dependency_cycle_naming_the_offending_nodes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow_file = tmp_path / "workflow.yaml"
    workflow_file.write_text(
        "name: sample\n"
        "version: 1\n"
        "nodes:\n"
        "  - id: a\n"
        "    kind: shell\n"
        "    needs: [b]\n"
        "    inputs:\n"
        "      command: echo a\n"
        "  - id: b\n"
        "    kind: shell\n"
        "    needs: [a]\n"
        "    inputs:\n"
        "      command: echo b\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["run", str(workflow_file)])

    assert result.exit_code == 2
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "workflow.yaml" in result.output
    assert "dependency cycle: a -> b -> a" in result.output, "the cycle members are named"
    assert not (tmp_path / ".caw").exists()


def test_run_rejects_an_unknown_node_kind_naming_file_and_node(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow_file = tmp_path / "workflow.yaml"
    workflow_file.write_text(
        "name: sample\n"
        "version: 1\n"
        "nodes:\n"
        "  - id: greet\n"
        "    kind: rocket\n"
        "    inputs:\n"
        "      command: echo hello\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["run", str(workflow_file)])

    assert result.exit_code == 2
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "workflow.yaml" in result.output, "the error names the workflow file"
    assert "greet" in result.output, "the error names the node id, not just its index"
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
