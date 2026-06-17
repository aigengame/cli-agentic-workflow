"""CLI-seam tests: invoke the caw CLI and assert exit codes and stdout."""

import importlib.metadata
import json
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from caw.cli import app

runner = CliRunner()


def linear_pipeline(marker_command: str = "echo hello") -> dict[str, Any]:
    """A three-node linear pipeline build -> test -> deploy, declared non-topologically.

    Declaration order deliberately differs from dependency order so that any
    consumer falling back to declaration order fails the seam tests.
    """
    return {
        "name": "sample",
        "version": 1,
        "nodes": [
            {
                "id": "deploy",
                "kind": "shell",
                "needs": ["test"],
                "inputs": {"command": marker_command},
            },
            {"id": "build", "kind": "shell", "inputs": {"command": marker_command}},
            {
                "id": "test",
                "kind": "shell",
                "needs": ["build"],
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


def two_node_cycle() -> dict[str, Any]:
    """A minimal a <-> b dependency cycle: a needs b, b needs a."""
    return {
        "name": "sample",
        "version": 1,
        "nodes": [
            {"id": "a", "kind": "shell", "needs": ["b"], "inputs": {"command": "echo a"}},
            {"id": "b", "kind": "shell", "needs": ["a"], "inputs": {"command": "echo b"}},
        ],
    }


def test_validate_invalid_workflow_exits_two_with_a_single_error_line(
    write_workflow_data: Callable[[dict[str, Any]], Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow_file = write_workflow_data(two_node_cycle())
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


def test_version_flag_prints_the_installed_version_and_exits_zero() -> None:
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0, result.output
    # The flag reports the version resolved from the installed dist metadata
    # (built from pyproject.toml) -- never a hardcoded copy (#113).
    assert importlib.metadata.version("caw") in result.output


def test_version_short_flag_matches_the_long_flag() -> None:
    long = runner.invoke(app, ["--version"])
    short = runner.invoke(app, ["-V"])

    assert short.exit_code == 0, short.output
    assert short.output == long.output


def test_version_short_circuits_before_subcommand_dispatch() -> None:
    # --version is eager: it prints and exits without requiring a subcommand
    # and without falling through to the no_args_is_help screen.
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0, result.output
    assert "Usage" not in result.output


def test_version_help_lists_the_version_option() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "--version" in result.output


def test_version_via_installed_console_script() -> None:
    # Stronger than the in-process CliRunner checks: exercises the real installed
    # `caw` entry point so importlib.metadata resolution is confirmed end to end.
    caw_bin = shutil.which("caw")
    if caw_bin is None:
        pytest.skip("caw console script is not on PATH in this environment")

    proc = subprocess.run([caw_bin, "--version"], capture_output=True, text=True)

    assert proc.returncode == 0, proc.stderr
    assert importlib.metadata.version("caw") in proc.stdout


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


def test_graph_text_plan_shows_the_concurrency_limit(
    write_workflow_data: Callable[[dict[str, Any]], Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #55.2: the JSON plan exposes `concurrency`, but the human-readable text plan
    # did not, so the two formats disagreed and a user inspecting the text plan
    # got no feedback that a non-default concurrency limit took effect.
    plan_input = linear_pipeline()
    plan_input["concurrency"] = 3
    workflow_file = write_workflow_data(plan_input)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["graph", str(workflow_file)])

    assert result.exit_code == 0, result.output
    assert "concurrency: 3" in result.output, (
        "the text plan names the concurrency setting and its configured value"
    )


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
        {"id": "deploy", "kind": "shell", "needs": ["test"], "when": None, "join": "all"},
        {"id": "build", "kind": "shell", "needs": [], "when": None, "join": "all"},
        {"id": "test", "kind": "shell", "needs": ["build"], "when": None, "join": "all"},
    ], "nodes render in declaration order; unconditional nodes show when: null, join: all"
    assert plan["edges"] == [
        {"from": "test", "to": "deploy"},
        {"from": "build", "to": "test"},
    ]
    assert plan["topological_order"] == ["build", "test", "deploy"], (
        "the plan names the order's semantics: a topological linearization, "
        "not a promise of sequential execution"
    )
    assert "order" not in plan, "the unqualified key is gone before external consumers exist"
    assert not (tmp_path / ".caw").exists()


def test_graph_json_plan_surfaces_the_concurrency_limit(
    write_workflow_data: Callable[[dict[str, Any]], Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_input = linear_pipeline()
    plan_input["concurrency"] = 3
    workflow_file = write_workflow_data(plan_input)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["graph", str(workflow_file), "--format", "json"])

    assert result.exit_code == 0, result.output
    plan = json.loads(result.output)
    assert plan["concurrency"] == 3, "the machine-readable plan exposes the concurrency limit"


def test_graph_json_plan_defaults_concurrency_when_unspecified(
    write_workflow_data: Callable[[dict[str, Any]], Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow_file = write_workflow_data(linear_pipeline())
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["graph", str(workflow_file), "--format", "json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["concurrency"] == 4, "the plan shows the conservative default"


def conditional_pipeline() -> dict[str, Any]:
    """A classify-and-act pipeline whose `act` carries a `when` and a `join: any`.

    Exercises the AC5 graph surfacing: a Node that gates on an upstream field and
    tolerates skipped branches must show both its `when` predicate and its `join`
    policy in the plan, distinctly from an ordinary unconditional Node.
    """
    return {
        "name": "sample",
        "version": 1,
        "nodes": [
            {"id": "classify", "kind": "shell", "inputs": {"command": "echo billing"}},
            {
                "id": "act",
                "kind": "shell",
                "needs": ["classify"],
                "join": "any",
                "when": {
                    "ref": {"node": "classify", "field": "stdout"},
                    "op": "equals",
                    "value": "shipping",
                },
                "inputs": {"command": "echo act"},
            },
        ],
    }


def test_graph_json_plan_surfaces_each_nodes_when_predicate_and_join_policy(
    write_workflow_data: Callable[[dict[str, Any]], Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # AC5 (#7), machine-readable: the JSON plan must expose each Node's `when`
    # predicate and `join` policy so a consumer can see the conditional structure
    # without running the workflow. The conditional `act` shows its predicate (the
    # serialized algebra, round-tripping the `not`/leaf shape) and `join: any`; the
    # unconditional `classify` shows `when: null` and the default `join: all`.
    workflow_file = write_workflow_data(conditional_pipeline())
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["graph", str(workflow_file), "--format", "json"])

    assert result.exit_code == 0, result.output
    nodes = {node["id"]: node for node in json.loads(result.output)["nodes"]}
    assert nodes["classify"]["when"] is None, "an unconditional node has no predicate"
    assert nodes["classify"]["join"] == "all", "join defaults to all in the plan"
    assert nodes["act"]["join"] == "any", "the tolerant join policy is surfaced"
    assert nodes["act"]["when"] == {
        "ref": {"node": "classify", "field": "stdout"},
        "op": "equals",
        "value": "shipping",
    }, "the predicate is surfaced as the serialized algebra"


def test_graph_json_plan_preserves_a_falsy_when_value(
    write_workflow_data: Callable[[dict[str, Any]], Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #77: the predicate serializer must preserve a leaf's MEANINGFUL falsy value —
    # `exit_status equals 0` — not strip it as an inactive-shape None. `value: null`
    # is moot here (it is rejected at validation per #75's decision), so the concrete
    # hazard the JSON plan must survive is the falsy-but-valid `0`, which the old
    # `exclude_none` happened to keep but `to_plan_dict` keeps by design.
    plan_input: dict[str, Any] = {
        "name": "sample",
        "version": 1,
        "nodes": [
            {"id": "probe", "kind": "shell", "inputs": {"command": "true"}},
            {
                "id": "act",
                "kind": "shell",
                "needs": ["probe"],
                "when": {
                    "ref": {"node": "probe", "field": "exit_status"},
                    "op": "equals",
                    "value": 0,
                },
                "inputs": {"command": "echo act"},
            },
        ],
    }
    workflow_file = write_workflow_data(plan_input)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["graph", str(workflow_file), "--format", "json"])

    assert result.exit_code == 0, result.output
    nodes = {node["id"]: node for node in json.loads(result.output)["nodes"]}
    assert nodes["act"]["when"] == {
        "ref": {"node": "probe", "field": "exit_status"},
        "op": "equals",
        "value": 0,
    }, "a meaningful falsy `value: 0` survives serialization, not stripped"


def test_graph_json_plan_serializes_a_structured_output_sub_path(
    write_workflow_data: Callable[[dict[str, Any]], Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #75/#77: a structured_output sub-`path` leaf serializes its `path` in the plan
    # so a consumer sees the routing target; a leaf with NO sub-path carries no
    # spurious empty `path` key (proven by the other plan tests). The classifier is
    # an agent Node (only kind that emits structured_output).
    plan_input: dict[str, Any] = {
        "name": "sample",
        "version": 1,
        "nodes": [
            {
                "id": "classify",
                "kind": "agent",
                "inputs": {"adapter": "mock", "prompt": "classify"},
            },
            {
                "id": "act",
                "kind": "shell",
                "needs": ["classify"],
                "when": {
                    "ref": {
                        "node": "classify",
                        "field": "structured_output",
                        "path": ["category"],
                    },
                    "op": "equals",
                    "value": "bug",
                },
                "inputs": {"command": "echo act"},
            },
        ],
    }
    workflow_file = write_workflow_data(plan_input)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["graph", str(workflow_file), "--format", "json"])

    assert result.exit_code == 0, result.output
    nodes = {node["id"]: node for node in json.loads(result.output)["nodes"]}
    assert nodes["act"]["when"]["ref"] == {
        "node": "classify",
        "field": "structured_output",
        "path": ["category"],
    }, "the structured_output sub-path is serialized so a consumer sees the routing target"


def test_graph_text_plan_annotates_a_nodes_when_and_non_default_join(
    write_workflow_data: Callable[[dict[str, Any]], Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # AC5 (#7), human-readable: the text plan must annotate a conditional Node so a
    # user reading the plan sees it carries a `when` gate and a non-default `join`
    # policy. `act` shows both annotations; an unconditional default-join node
    # carries neither, so the annotations are additive noise-free signal.
    workflow_file = write_workflow_data(conditional_pipeline())
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["graph", str(workflow_file)])

    assert result.exit_code == 0, result.output
    act_line = next(line for line in result.output.splitlines() if "act" in line)
    assert "when" in act_line, "the conditional node is annotated with its `when` gate"
    assert "join: any" in act_line, "the non-default join policy is annotated"
    classify_line = next(line for line in result.output.splitlines() if "classify" in line)
    assert "when" not in classify_line, "an unconditional node carries no `when` annotation"
    assert "join" not in classify_line, "a default-join node carries no `join` annotation"


def test_run_rejects_a_concurrency_below_one_with_a_single_error_line(
    write_workflow_data: Callable[[dict[str, Any]], Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bad = linear_pipeline()
    bad["concurrency"] = 0
    workflow_file = write_workflow_data(bad)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["run", str(workflow_file)])

    assert result.exit_code == 2, "a concurrency below 1 is a config error"
    assert result.exception is None or isinstance(result.exception, SystemExit)
    lines = [line for line in result.output.splitlines() if line.strip()]
    assert len(lines) == 1
    assert lines[0].startswith("error:")
    assert "concurrency" in lines[0], "the error names the offending field"
    assert not (tmp_path / ".caw").exists(), "no run directory is created for invalid config"


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


def test_run_output_lists_a_skipped_join_node_and_its_blocker(
    write_workflow_data: Callable[[dict[str, Any]], Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #55.1: a fan-in where one branch fails skips the join, which is recorded
    # `skipped` (with its blocker) in State and Events but was invisible in the
    # run summary, which iterated only attempted nodes. The summary must list the
    # skipped join distinctly from the attempted branches, naming the node that
    # blocked it, so a user can tell withheld work from work that was never run.
    workflow_file = write_workflow_data(
        {
            "name": "sample",
            "version": 1,
            "nodes": [
                {"id": "left", "kind": "shell", "inputs": {"command": "echo left"}},
                {"id": "right", "kind": "shell", "inputs": {"command": "exit 7"}},
                {
                    "id": "join",
                    "kind": "shell",
                    "needs": ["left", "right"],
                    "inputs": {"command": "echo join"},
                },
            ],
        }
    )
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["run", str(workflow_file)])

    assert result.exit_code == 1, result.output
    assert "node left attempt 1 exited 0" in result.output, "an attempted branch is reported"
    assert "node right attempt 1 exited 7" in result.output, "the failed branch is reported"
    lines = result.output.splitlines()
    skipped_line = next((line for line in lines if "join" in line and "skipped" in line), None)
    assert skipped_line is not None, "the skipped join node appears in the run summary"
    assert "right" in skipped_line, "the skipped node names the branch that blocked it"


def test_run_output_distinguishes_a_when_skip_from_a_blocked_skip(
    write_workflow_data: Callable[[dict[str, Any]], Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # AC5 (#7): a Node skipped by a closed `when` gate must read distinctly from
    # one withheld by a failure, and BOTH distinctly from success/failure. Here
    # `gate` is skipped `when_false` (its own gate closed) while `downstream`,
    # needing `gate`, is skipped `blocked`. The summary must render the when-skip
    # without a "blocked by" phrase and the blocked-skip naming its blocker, so a
    # user can tell a closed condition from withheld-by-failure work.
    workflow_file = write_workflow_data(
        {
            "name": "sample",
            "version": 1,
            "nodes": [
                {"id": "classify", "kind": "shell", "inputs": {"command": "echo billing"}},
                {
                    "id": "gate",
                    "kind": "shell",
                    "needs": ["classify"],
                    "when": {
                        "ref": {"node": "classify", "field": "stdout"},
                        "op": "equals",
                        "value": "shipping",
                    },
                    "inputs": {"command": "echo gate"},
                },
                {
                    "id": "downstream",
                    "kind": "shell",
                    "needs": ["gate"],
                    "inputs": {"command": "echo downstream"},
                },
            ],
        }
    )
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["run", str(workflow_file)])

    assert result.exit_code == 0, "a benign when-skip does not fail the run"
    lines = result.output.splitlines()
    gate_line = next(line for line in lines if "gate" in line and "skipped" in line)
    downstream_line = next(line for line in lines if "downstream" in line and "skipped" in line)
    assert "when" in gate_line, "a closed-gate skip names its cause distinctly"
    assert "blocked by" not in gate_line, "a when-skip is not a blocked-by-failure skip"
    assert "blocked by gate" in downstream_line, "a blocked skip still names its blocker"
    assert "succeeded" in result.output and "failed" not in result.output, (
        "a skip reads distinctly from success and failure"
    )


def _run_dir_name(tmp_path: Path) -> str:
    run_dirs = list((tmp_path / ".caw" / "runs").iterdir())
    assert len(run_dirs) == 1
    return run_dirs[0].name


def test_resume_completes_an_interrupted_run_and_exits_zero(
    write_workflow_data: Callable[[dict[str, Any]], Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Acceptance criterion #6.4 at the CLI seam: `caw resume <run-id>` continues an
    # interrupted run to completion, re-running only incomplete nodes, mirroring
    # `run`'s exit-code contract (0 on success). `build` succeeds; `test` fails on
    # the first run (its marker is absent) so `deploy` is skipped and the run
    # fails (exit 1); on resume `test` succeeds and `deploy` runs, exiting 0.
    test_marker = tmp_path / "test.marker"
    deployed = tmp_path / "deployed"
    workflow_file = write_workflow_data(
        {
            "name": "sample",
            "version": 1,
            "nodes": [
                {"id": "build", "kind": "shell", "inputs": {"command": "echo build"}},
                {
                    "id": "test",
                    "kind": "shell",
                    "needs": ["build"],
                    "inputs": {
                        "command": (
                            f"if [ -e {test_marker} ]; then exit 0; "
                            f"else touch {test_marker}; exit 7; fi"
                        )
                    },
                },
                {
                    "id": "deploy",
                    "kind": "shell",
                    "needs": ["test"],
                    "inputs": {"command": f"touch {deployed}"},
                },
            ],
        }
    )
    monkeypatch.chdir(tmp_path)

    first = runner.invoke(app, ["run", str(workflow_file)])
    assert first.exit_code == 1, first.output
    run_id = _run_dir_name(tmp_path)
    assert not deployed.exists()

    resumed = runner.invoke(app, ["resume", run_id])

    assert resumed.exit_code == 0, resumed.output
    assert "succeeded" in resumed.output
    assert run_id in resumed.output, "the resume result names the run id"
    assert deployed.exists(), "deploy ran on resume"


def test_resume_an_already_succeeded_run_is_refused_with_one_error_line(
    write_workflow: Callable[[str], Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A succeeded run is not resumable; the CLI refuses it with a single `error:`
    # line and a config-class exit code, never re-running it.
    workflow_file = write_workflow("echo ok")
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["run", str(workflow_file)]).exit_code == 0
    run_id = _run_dir_name(tmp_path)

    result = runner.invoke(app, ["resume", run_id])

    assert result.exit_code == 2, "refusing an ineligible run is a config-class error"
    assert result.exception is None or isinstance(result.exception, SystemExit)
    lines = [line for line in result.output.splitlines() if line.strip()]
    assert len(lines) == 1
    assert lines[0].startswith("error:")
    assert "not resumable" in lines[0]


def test_resume_an_unknown_run_id_is_refused_with_one_error_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["resume", "no-such-run"])

    assert result.exit_code == 2
    assert result.exception is None or isinstance(result.exception, SystemExit)
    lines = [line for line in result.output.splitlines() if line.strip()]
    assert len(lines) == 1
    assert lines[0].startswith("error:")
    assert "no-such-run" in lines[0], "the error names the unknown run id"


def test_run_reports_the_real_attempt_number_after_a_retry(
    write_workflow_data: Callable[[dict[str, Any]], Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The run summary names the Attempt a Node actually finished on, not a
    # hardcoded "attempt 1" (#6): a node that fails once then succeeds reports
    # "attempt 2", so a reader sees the retry happened.
    marker = tmp_path / "marker"
    command = f"if [ -e {marker} ]; then exit 0; else touch {marker}; exit 7; fi"
    workflow_file = write_workflow_data(
        {
            "name": "sample",
            "version": 1,
            "nodes": [
                {"id": "flaky", "kind": "shell", "retries": 1, "inputs": {"command": command}}
            ],
        }
    )
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["run", str(workflow_file)])

    assert result.exit_code == 0, result.output
    assert "node flaky attempt 2 exited 0" in result.output, "the report names the real attempt"


def test_run_failure_message_names_the_workflow_file_node_id_and_adapter(
    write_workflow_data: Callable[[dict[str, Any]], Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Acceptance criterion #6.5: a node failure's surfaced message names the
    # workflow file, the node id, and (for an agent node) the adapter, so a user
    # can locate the failure across definition, graph, and integration without
    # cross-referencing. The mock agent node fails (fixture exit_status 7); the
    # run output must mention all three identifiers.
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps({"exit_status": 7, "stderr": "agent blew up"}), encoding="utf-8")
    workflow_file = write_workflow_data(
        {
            "name": "sample",
            "version": 1,
            "nodes": [
                {
                    "id": "summarize",
                    "kind": "agent",
                    "inputs": {
                        "adapter": "mock",
                        "prompt": "do it",
                        "fixture": str(fixture),
                    },
                }
            ],
        }
    )
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["run", str(workflow_file)])

    assert result.exit_code == 1, result.output
    assert "workflow.yaml" in result.output, "the failure names the workflow file"
    assert "summarize" in result.output, "the failure names the node id"
    assert "mock" in result.output, "the failure names the adapter"


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
    write_workflow_data: Callable[[dict[str, Any]], Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow_file = write_workflow_data(two_node_cycle())
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["run", str(workflow_file)])

    assert result.exit_code == 2
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "workflow.yaml" in result.output
    assert "dependency cycle: 'a' -> 'b' -> 'a'" in result.output, "the cycle members are named"
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


def test_validate_unknown_adapter_name_is_a_config_error_naming_the_node(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #64a: a typo'd / unknown adapter name must fail `caw validate` (exit 2, one
    # error line naming the node) against the built-in adapter set, before any run
    # directory is created — fail-fast rather than spending upstream tokens then
    # failing the node at run time.
    workflow_file = tmp_path / "agent.yaml"
    workflow_file.write_text(
        "name: sample\n"
        "version: 1\n"
        "nodes:\n"
        "  - id: summarize\n"
        "    kind: agent\n"
        "    inputs:\n"
        "      adapter: claued\n"  # typo for a built-in
        "      prompt: do it\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["validate", str(workflow_file)])

    assert result.exit_code == 2
    assert result.exception is None or isinstance(result.exception, SystemExit)
    lines = [line for line in result.output.splitlines() if line.strip()]
    assert len(lines) == 1
    assert lines[0].startswith("error:")
    assert "summarize" in lines[0], "the error names the node"
    assert "claued" in lines[0], "the error names the unknown adapter"
    assert not (tmp_path / ".caw").exists(), "no run directory is created"


def test_run_relative_schema_and_fixture_paths_resolve_against_the_workflow_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #64b: relative output_schema / fixture paths resolve relative to the workflow
    # FILE's directory, not the process CWD, so the same definition runs identically
    # regardless of the invocation directory. Author the workflow and its sidecar
    # files under `project/`, then run from an UNRELATED cwd.
    project = tmp_path / "project"
    project.mkdir()
    (project / "fixture.json").write_text(
        json.dumps({"exit_status": 0, "structured_output": {"summary": "s"}}),
        encoding="utf-8",
    )
    (project / "schema.json").write_text(
        json.dumps({"type": "object", "required": ["summary"]}), encoding="utf-8"
    )
    workflow_file = project / "workflow.yaml"
    workflow_file.write_text(
        "name: sample\n"
        "version: 1\n"
        "nodes:\n"
        "  - id: summarize\n"
        "    kind: agent\n"
        "    inputs:\n"
        "      adapter: mock\n"
        "      prompt: do it\n"
        "      fixture: fixture.json\n"  # relative to the workflow file
        "      output_schema: schema.json\n",
        encoding="utf-8",
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    result = runner.invoke(app, ["run", str(workflow_file)])

    assert result.exit_code == 0, result.output
    assert "succeeded" in result.output
    # The run directory is rooted at the invocation cwd, not the workflow dir.
    assert (elsewhere / ".caw").exists()
    assert not (project / ".caw").exists()


def test_run_malformed_agent_node_is_a_config_error_before_executing_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow_file = tmp_path / "agent.yaml"
    # An agent Node missing its required `adapter` is malformed; it must fail as a
    # config error (exit 2, one error line naming the node) before any execution,
    # exactly like a malformed shell Node.
    workflow_file.write_text(
        "name: sample\n"
        "version: 1\n"
        "nodes:\n"
        "  - id: summarize\n    kind: agent\n    inputs:\n      prompt: do it\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["run", str(workflow_file)])

    assert result.exit_code == 2
    assert result.exception is None or isinstance(result.exception, SystemExit)
    lines = [line for line in result.output.splitlines() if line.strip()]
    assert len(lines) == 1
    assert lines[0].startswith("error:")
    assert "summarize" in lines[0], "the error names the node id"
    assert not (tmp_path / ".caw").exists(), "no run directory is created for invalid input"
