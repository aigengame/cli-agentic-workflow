"""Aggregate report over a Run Group (#15, AC6, ADR 0002 / 0009).

`caw report` over a Run Group aggregates ALL iterations into one result, keeping
each iteration's conclusion distinct from its trace evidence — rendered purely
from persisted State and Events, never re-executing. These tests drive a finished
group offline with the mock Adapter, then render the aggregate report.
"""

import json
from pathlib import Path

import pytest

from caw.controller import ControllerSpec, run_loop_until_done
from caw.report import ReportFormat, render_group_report


def _write_fixture(path: Path, *, done: bool, next_fixture: str | None = None) -> None:
    structured: dict[str, object] = {}
    if next_fixture is not None:
        structured["next_fixture"] = next_fixture
    path.write_text(
        json.dumps(
            {
                "exit_status": 0,
                "stdout": "FINISHED" if done else "CONTINUE",
                "structured_output": structured,
            }
        ),
        encoding="utf-8",
    )


def _write_workflow(directory: Path, first_fixture: str) -> Path:
    workflow = directory / "iteration.yaml"
    workflow.write_text(
        "name: loop-iteration\n"
        "version: 1\n"
        "nodes:\n"
        "  - id: verdict\n"
        "    kind: agent\n"
        "    inputs:\n"
        "      adapter: mock\n"
        "      prompt: Decide whether the task is done.\n"
        f"      fixture: {first_fixture}\n",
        encoding="utf-8",
    )
    return workflow


def _spec(workflow: Path) -> ControllerSpec:
    return ControllerSpec.model_validate(
        {
            "workflow": str(workflow),
            "max_iterations": 5,
            "evaluate_node": "verdict",
            "done": {
                "ref": {"node": "verdict", "field": "stdout"},
                "op": "contains",
                "value": "FINISHED",
            },
            "feedback": {
                "to_node": "verdict",
                "to_field": "fixture",
                "from_field": "next_fixture",
            },
        }
    )


async def _run_two_iteration_group(tmp_path: Path) -> str:
    _write_fixture(tmp_path / "iter1.fixture.json", done=False, next_fixture="iter2.fixture.json")
    _write_fixture(tmp_path / "iter2.fixture.json", done=True)
    workflow = _write_workflow(tmp_path, "iter1.fixture.json")
    result = await run_loop_until_done(_spec(workflow), base=tmp_path)
    assert result.status == "done"
    assert len(result.iterations) == 2
    return result.group_id


@pytest.mark.asyncio
async def test_json_group_report_aggregates_every_iteration(tmp_path: Path) -> None:
    # AC6: the aggregate JSON report carries the group status and EVERY iteration's
    # per-run conclusion, in order, each with its own run id and node statuses.
    group_id = await _run_two_iteration_group(tmp_path)

    rendered = render_group_report(group_id, tmp_path, ReportFormat.json)
    report = json.loads(rendered)

    assert report["group_id"] == group_id
    assert report["status"] == "done"
    assert len(report["iterations"]) == 2, "all iterations aggregated into one result"
    for index, iteration in enumerate(report["iterations"]):
        assert iteration["iteration_index"] == index
        assert iteration["status"] == "succeeded"
        node_ids = [node["id"] for node in iteration["nodes"]]
        assert "verdict" in node_ids, "each iteration's per-run conclusion is included"


@pytest.mark.asyncio
async def test_group_report_includes_trace_evidence_per_iteration(tmp_path: Path) -> None:
    # AC6: the aggregate keeps each iteration's CONCLUSION distinct from its TRACE
    # evidence (the append-only events), so the group report carries provenance.
    group_id = await _run_two_iteration_group(tmp_path)

    report = json.loads(render_group_report(group_id, tmp_path, ReportFormat.json))

    for iteration in report["iterations"]:
        assert iteration["trace"], "each iteration carries its append-only event trace"
        event_types = {event["type"] for event in iteration["trace"]}
        assert "run_started" in event_types, "the iteration's run-start event is traced"


@pytest.mark.asyncio
async def test_markdown_group_report_renders_each_iteration(tmp_path: Path) -> None:
    # A human-facing aggregate: the group's status and a per-iteration section.
    group_id = await _run_two_iteration_group(tmp_path)

    rendered = render_group_report(group_id, tmp_path, ReportFormat.markdown)

    assert f"# Run Group {group_id}" in rendered
    assert "**Status:** done" in rendered
    assert "## Iteration 0" in rendered
    assert "## Iteration 1" in rendered
