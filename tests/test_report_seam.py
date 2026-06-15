"""Report-seam tests: invoke `caw report` and assert it renders from persisted state."""

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from caw.cli import app

runner = CliRunner()


def _run_dir_name(tmp_path: Path) -> str:
    run_dirs = list((tmp_path / ".caw" / "runs").iterdir())
    assert len(run_dirs) == 1
    return run_dirs[0].name


def test_report_json_renders_conclusion_and_trace_for_a_completed_run(
    write_workflow: Callable[[str], Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #12 tracer bullet: `caw report <id> --format json` renders from persisted
    # State and Events only — the run's conclusion (the run and its node statuses)
    # kept distinct from the trace (the append-only event sequence).
    workflow_file = write_workflow("echo hello")
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["run", str(workflow_file)]).exit_code == 0
    run_id = _run_dir_name(tmp_path)

    result = runner.invoke(app, ["report", run_id, "--format", "json"])

    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    # Conclusion: the run-level status and each node's status.
    assert report["run_id"] == run_id
    assert report["status"] == "succeeded"
    assert {node["id"]: node["status"] for node in report["nodes"]} == {"greet": "succeeded"}
    # Trace: the persisted event sequence, kept separate from the conclusion.
    trace_types = [event["type"] for event in report["trace"]]
    assert trace_types[0] == "run_started"
    assert "run_finished" in trace_types


def test_report_json_surfaces_an_agent_nodes_structured_output(
    write_workflow_data: Callable[[dict[str, Any]], Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #12: an agent node's conclusion includes its persisted structured_output, so a
    # report surfaces the agent's typed result, not just its status. Driven offline
    # through the mock Adapter (a fixture replays the structured output); the real
    # claude.print shape is confirmed by the e2e suite.
    fixture = tmp_path / "classify.fixture.json"
    fixture.write_text(
        json.dumps({"exit_status": 0, "stdout": "ok", "structured_output": {"category": "bug"}}),
        encoding="utf-8",
    )
    workflow_file = write_workflow_data(
        {
            "name": "sample",
            "version": 1,
            "nodes": [
                {
                    "id": "classify",
                    "kind": "agent",
                    "inputs": {"adapter": "mock", "prompt": "classify", "fixture": str(fixture)},
                }
            ],
        }
    )
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["run", str(workflow_file)]).exit_code == 0
    run_id = _run_dir_name(tmp_path)

    report = json.loads(runner.invoke(app, ["report", run_id, "--format", "json"]).output)
    classify = next(node for node in report["nodes"] if node["id"] == "classify")
    assert classify["structured_output"] == {"category": "bug"}


def test_report_renders_a_parked_run_status_agnostically(
    write_workflow: Callable[[str], Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #12 AC: reports must work for parked (awaiting approval) runs. The human gate
    # (#10) is not built yet, so we simulate its persisted state — an `awaiting` run
    # status — and assert the reporter renders it verbatim without crashing, rather
    # than special-casing a closed set of statuses.
    workflow_file = write_workflow("echo hi")
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["run", str(workflow_file)]).exit_code == 0
    run_id = _run_dir_name(tmp_path)
    database = tmp_path / ".caw" / "runs" / run_id / "state.sqlite"
    connection = sqlite3.connect(database)
    connection.execute("UPDATE run SET status = 'awaiting' WHERE run_id = ?", (run_id,))
    connection.commit()
    connection.close()

    result = runner.invoke(app, ["report", run_id, "--format", "markdown"])

    assert result.exit_code == 0, result.output
    assert "**Status:** awaiting" in result.output
    report = json.loads(runner.invoke(app, ["report", run_id, "--format", "json"]).output)
    assert report["status"] == "awaiting"


def test_report_surfaces_a_failed_runs_status_and_errors(
    write_workflow_data: Callable[[dict[str, Any]], Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #12: a report renders a failed run too — the run reads `failed`, the failing
    # node names its exit status, and its stderr surfaces under the Errors section.
    workflow_file = write_workflow_data(
        {
            "name": "sample",
            "version": 1,
            "nodes": [
                {"id": "boom", "kind": "shell", "inputs": {"command": "echo kaboom >&2; exit 3"}}
            ],
        }
    )
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["run", str(workflow_file)]).exit_code == 1
    run_id = _run_dir_name(tmp_path)

    markdown = runner.invoke(app, ["report", run_id, "--format", "markdown"])
    assert markdown.exit_code == 0, markdown.output
    assert "**Status:** failed" in markdown.output
    assert "boom — failed (exit 3)" in markdown.output
    errors_section = markdown.output.split("## Errors")[1].split("## Trace")[0]
    assert "boom" in errors_section and "kaboom" in errors_section

    report = json.loads(runner.invoke(app, ["report", run_id, "--format", "json"]).output)
    assert report["status"] == "failed"
    boom = next(node for node in report["nodes"] if node["id"] == "boom")
    assert boom["status"] == "failed"
    assert boom["exit_status"] == 3
    assert "kaboom" in boom["error"]


def test_report_unknown_run_id_is_refused_with_one_error_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #12: reporting an unknown run id is refused like `caw resume` — a single
    # `error:` line naming the run id and a config-class exit code, never a traceback.
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["report", "no-such-run"])

    assert result.exit_code == 2
    assert result.exception is None or isinstance(result.exception, SystemExit)
    lines = [line for line in result.output.splitlines() if line.strip()]
    assert len(lines) == 1
    assert lines[0].startswith("error:")
    assert "no-such-run" in lines[0], "the error names the unknown run id"


def test_report_jsonl_streams_a_conclusion_record_then_event_records(
    write_workflow: Callable[[str], Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #12: JSONL is the line-delimited stream — a leading conclusion record, then
    # one record per event, each tagged so conclusion stays distinct from trace.
    workflow_file = write_workflow("echo hello")
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["run", str(workflow_file)]).exit_code == 0
    run_id = _run_dir_name(tmp_path)

    result = runner.invoke(app, ["report", run_id, "--format", "jsonl"])

    assert result.exit_code == 0, result.output
    records = [json.loads(line) for line in result.output.splitlines() if line.strip()]
    assert records[0]["record"] == "conclusion"
    assert records[0]["run_id"] == run_id
    assert records[0]["status"] == "succeeded"
    assert {node["id"]: node["status"] for node in records[0]["nodes"]} == {"greet": "succeeded"}
    events = [record for record in records[1:] if record["record"] == "event"]
    assert events[0]["type"] == "run_started"
    assert any(event["type"] == "run_finished" for event in events)


def test_report_markdown_includes_graph_statuses_artifacts_and_errors(
    write_workflow_data: Callable[[dict[str, Any]], Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #12 AC: the Markdown report includes the graph, node statuses, artifact
    # references, and errors — each under its own section, conclusion before trace.
    workflow_file = write_workflow_data(
        {
            "name": "sample",
            "version": 1,
            "nodes": [
                {"id": "build", "kind": "shell", "inputs": {"command": "echo build"}},
                {
                    "id": "ship",
                    "kind": "shell",
                    "needs": ["build"],
                    "inputs": {"command": "echo ship"},
                },
            ],
        }
    )
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["run", str(workflow_file)]).exit_code == 0
    run_id = _run_dir_name(tmp_path)

    result = runner.invoke(app, ["report", run_id, "--format", "markdown"])

    assert result.exit_code == 0, result.output
    md = result.output
    assert f"# Run {run_id}" in md
    assert "**Status:** succeeded" in md
    # Graph: nodes in declaration order with their dependency edges.
    assert "## Graph" in md
    assert "ship (needs: build)" in md
    # Node statuses.
    assert "## Nodes" in md
    assert "build — succeeded (exit 0)" in md
    assert "ship — succeeded (exit 0)" in md
    # Artifact references and errors each get a section (empty here, but present).
    assert "## Artifacts" in md
    assert "## Errors" in md
    # Trace evidence, below the conclusion.
    assert md.index("## Errors") < md.index("## Trace")
    assert "run_started" in md


def test_report_text_separates_conclusion_from_trace_for_a_completed_run(
    write_workflow: Callable[[str], Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #12: the plain-text report leads with the conclusion (run + node statuses,
    # each node's exit status) and renders the trace below it, distinctly labelled.
    workflow_file = write_workflow("echo hello")
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["run", str(workflow_file)]).exit_code == 0
    run_id = _run_dir_name(tmp_path)

    result = runner.invoke(app, ["report", run_id, "--format", "text"])

    assert result.exit_code == 0, result.output
    out = result.output
    conclusion, _, trace = out.partition("trace")
    # Conclusion section: the run-level status and each node's status + exit.
    assert f"run {run_id}: succeeded" in conclusion
    assert "greet: succeeded (exit 0)" in conclusion
    # Trace section: the event sequence, below and separate from the conclusion.
    assert trace, "the text report has a distinct trace section"
    assert "run_started" in trace
    assert "run_finished" in trace
