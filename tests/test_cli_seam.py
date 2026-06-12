"""CLI-seam tests: invoke the caw CLI and assert exit codes and stdout."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from caw.cli import app

runner = CliRunner()


def linear_pipeline(marker_command: str = "echo hello") -> dict[str, Any]:
    """A sample three-node linear pipeline: build -> test -> deploy."""
    return {
        "name": "sample",
        "version": 1,
        "nodes": [
            {"id": "build", "kind": "shell", "inputs": {"command": marker_command}},
            {
                "id": "test",
                "kind": "shell",
                "needs": ["build"],
                "inputs": {"command": marker_command},
            },
            {
                "id": "deploy",
                "kind": "shell",
                "needs": ["test"],
                "inputs": {"command": marker_command},
            },
        ],
    }


def test_validate_a_valid_pipeline_exits_zero_and_executes_nothing(
    write_workflow_data: Callable[[dict[str, Any]], Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "marker.txt"
    workflow_file = write_workflow_data(linear_pipeline(f"touch {marker}"))
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["validate", str(workflow_file)])

    assert result.exit_code == 0, result.output
    assert not marker.exists(), "validate never executes a node"
    assert not (tmp_path / ".caw").exists(), "validate never creates a run directory"


def test_validate_reports_one_ok_line_naming_the_workflow_file(
    write_workflow_data: Callable[[dict[str, Any]], Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow_file = write_workflow_data(linear_pipeline())
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["validate", str(workflow_file)])

    assert result.exit_code == 0
    lines = [line for line in result.output.splitlines() if line.strip()]
    assert len(lines) == 1
    assert "valid" in lines[0]
    assert "workflow.yaml" in lines[0]


def test_validate_invalid_workflow_exits_two_with_a_single_error_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow_file = tmp_path / "workflow.yaml"
    workflow_file.write_text(
        "name: sample\n"
        "version: 1\n"
        "nodes:\n"
        "  - id: a\n    kind: shell\n    needs: [b]\n    inputs:\n      command: echo a\n"
        "  - id: b\n    kind: shell\n    needs: [a]\n    inputs:\n      command: echo b\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["validate", str(workflow_file)])

    assert result.exit_code == 2
    assert result.exception is None or isinstance(result.exception, SystemExit)
    lines = [line for line in result.output.splitlines() if line.strip()]
    assert len(lines) == 1
    assert lines[0].startswith("error:")
    assert "dependency cycle" in lines[0]
    assert not (tmp_path / ".caw").exists()


def test_help_exits_zero_and_names_the_cli() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "caw" in result.output


def test_graph_renders_a_text_plan_in_execution_order_with_needs(
    write_workflow_data: Callable[[dict[str, Any]], Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow_file = write_workflow_data(
        {
            "name": "sample",
            "version": 1,
            "nodes": [
                {
                    "id": "deploy",
                    "kind": "shell",
                    "needs": ["test"],
                    "inputs": {"command": "echo deploy"},
                },
                {
                    "id": "test",
                    "kind": "shell",
                    "needs": ["build"],
                    "inputs": {"command": "echo test"},
                },
                {"id": "build", "kind": "shell", "inputs": {"command": "echo build"}},
            ],
        }
    )
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["graph", str(workflow_file)])

    assert result.exit_code == 0, result.output
    assert "sample" in result.output, "the plan names the workflow"
    assert (
        result.output.index("build") < result.output.index("test") < result.output.index("deploy")
    ), "nodes are listed in execution order, not declaration order"
    assert "needs: build" in result.output
    assert "needs: test" in result.output
    assert not (tmp_path / ".caw").exists(), "graph never creates a run directory"


def test_graph_renders_a_json_plan_with_nodes_edges_and_order(
    write_workflow_data: Callable[[dict[str, Any]], Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow_file = write_workflow_data(linear_pipeline())
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["graph", str(workflow_file), "--format", "json"])

    assert result.exit_code == 0, result.output
    plan = json.loads(result.output)
    assert plan["workflow"] == "sample"
    assert plan["nodes"] == [
        {"id": "build", "kind": "shell", "needs": []},
        {"id": "test", "kind": "shell", "needs": ["build"]},
        {"id": "deploy", "kind": "shell", "needs": ["test"]},
    ]
    assert plan["edges"] == [
        {"from": "build", "to": "test"},
        {"from": "test", "to": "deploy"},
    ]
    assert plan["order"] == ["build", "test", "deploy"]
    assert not (tmp_path / ".caw").exists()


def test_graph_invalid_workflow_exits_two_with_a_single_error_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow_file = tmp_path / "workflow.yaml"
    workflow_file.write_text(
        "name: sample\nversion: 1\nnodes:\n"
        "  - id: deploy\n    kind: shell\n    needs: [build]\n    inputs:\n"
        "      command: echo deploy\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["graph", str(workflow_file)])

    assert result.exit_code == 2
    assert result.exception is None or isinstance(result.exception, SystemExit)
    lines = [line for line in result.output.splitlines() if line.strip()]
    assert len(lines) == 1
    assert lines[0].startswith("error:")
    assert not (tmp_path / ".caw").exists()


def test_run_executes_a_multi_node_linear_workflow_in_dependency_order(
    write_workflow_data: Callable[[dict[str, Any]], Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = tmp_path / "order.log"
    pipeline = linear_pipeline()
    for node in pipeline["nodes"]:
        node["inputs"]["command"] = f"echo {node['id']} >> {log}"
    workflow_file = write_workflow_data(pipeline)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["run", str(workflow_file)])

    assert result.exit_code == 0, result.output
    assert "succeeded" in result.output
    assert log.read_text(encoding="utf-8").split() == ["build", "test", "deploy"]
    for node_id in ("build", "test", "deploy"):
        assert f"node {node_id} attempt 1 exited 0" in result.output


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


@pytest.mark.parametrize("command", ["run", "validate", "graph"])
def test_non_utf8_workflow_file_is_a_config_error_with_one_error_line(
    command: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow_file = tmp_path / "workflow.yaml"
    workflow_file.write_bytes(b"name: sample\nversion: 1\n\xff\xfe broken bytes\n")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, [command, str(workflow_file)])

    assert result.exit_code == 2, f"caw {command} must treat a non-UTF-8 file as a config error"
    assert result.exception is None or isinstance(result.exception, SystemExit)
    lines = [line for line in result.output.splitlines() if line.strip()]
    assert len(lines) == 1
    assert lines[0].startswith("error:")
    assert "workflow.yaml" in lines[0]
    assert not (tmp_path / ".caw").exists()


def test_usage_errors_exit_two_with_the_documented_framework_usage_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Documented carve-out (see the caw.cli docstring): CLI usage errors share
    # exit code 2 with config errors but render the framework's multi-line
    # usage message instead of a single `error:` line.
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["graph", "whatever.yaml", "--format", "yaml"])

    assert result.exit_code == 2
    assert "Invalid value" in result.output, "the framework names the rejected option value"
    assert "error:" not in result.output, "usage errors do not use the config-error line shape"
    assert not (tmp_path / ".caw").exists()


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


def test_run_rejects_duplicate_needs_entries_naming_node_and_duplicate(
    write_workflow_data: Callable[[dict[str, Any]], Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow_file = write_workflow_data(
        {
            "name": "sample",
            "version": 1,
            "nodes": [
                {"id": "build", "kind": "shell", "inputs": {"command": "echo build"}},
                {
                    "id": "test",
                    "kind": "shell",
                    "needs": ["build", "build"],
                    "inputs": {"command": "echo test"},
                },
            ],
        }
    )
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["run", str(workflow_file)])

    assert result.exit_code == 2
    assert result.exception is None or isinstance(result.exception, SystemExit)
    lines = [line for line in result.output.splitlines() if line.strip()]
    assert len(lines) == 1
    assert lines[0].startswith("error:")
    assert "test" in lines[0], "the error names the node with the duplicate entry"
    assert "build" in lines[0], "the error names the duplicated id"
    assert not (tmp_path / ".caw").exists()


@pytest.mark.parametrize(
    ("case", "needs_value"),
    [
        ("scalar string", "build"),
        ("integer", 123),
        ("null", None),
    ],
)
def test_run_rejects_non_list_needs_naming_the_contract_not_a_python_type(
    case: str,
    needs_value: Any,
    write_workflow_data: Callable[[dict[str, Any]], Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow_file = write_workflow_data(
        {
            "name": "sample",
            "version": 1,
            "nodes": [
                {"id": "build", "kind": "shell", "inputs": {"command": "echo build"}},
                {
                    "id": "test",
                    "kind": "shell",
                    "needs": needs_value,
                    "inputs": {"command": "echo test"},
                },
            ],
        }
    )
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["run", str(workflow_file)])

    assert result.exit_code == 2, f"a {case} needs value must be a config error"
    assert result.exception is None or isinstance(result.exception, SystemExit)
    lines = [line for line in result.output.splitlines() if line.strip()]
    assert len(lines) == 1
    assert lines[0].startswith("error:")
    assert "a list of node ids" in lines[0], "the error names the contract"
    assert "tuple" not in lines[0], "no Python type leaks into the YAML-facing message"
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
