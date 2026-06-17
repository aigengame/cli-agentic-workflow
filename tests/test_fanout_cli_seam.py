"""CLI-seam tests for the hand-written fan-out-synthesis sample (#14).

The fan-out-synthesis sample (``examples/fanout-synthesis/``) is the project's first
complete end-to-end agent sample: a HAND-WRITTEN workflow that fans the SAME task out
to two independent agent branches in parallel and joins BOTH branch outputs in a
``synthesize`` node (CONTEXT.md: Parallel). The offline ``fanout-synthesis.mock.yaml``
variant drives every node through the built-in ``mock`` adapter, replaying a companion
fixture, so it runs to success with no real Agent CLI and no tokens — the variant CI
and token-free evaluation run (issue AC2). The real dual-adapter variant
(``fanout-synthesis.real.yaml``, claude.print + codex.exec) is exercised by the e2e
suite.

These tests run the SHIPPED sample file through the public CLI (``caw run`` to success,
``caw graph`` for the shape, ``caw report`` for the conclusion/trace split) — never by
inspecting internals (project testing philosophy). They prove the sample is runnable
and that its Markdown report separates the final conclusion (``## Nodes``) from the
trace evidence (``## Trace``) — the issue's AC4.
"""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from caw.cli import app

runner = CliRunner()

# The shipped offline sample, located relative to the repo root (this file lives at
# tests/test_fanout_cli_seam.py). `caw run` anchors the sample's relative `fixture`
# paths to the YAML file's own directory (#64), so the bundle runs in place from any cwd.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_MOCK_SAMPLE = _REPO_ROOT / "examples" / "fanout-synthesis" / "fanout-synthesis.mock.yaml"
_BRANCH_IDS = ("claude_branch", "codex_branch")
_SYNTH_ID = "synthesize"


def test_mock_sample_validates_and_runs_to_success_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # AC1+AC2: the hand-written sample's mock variant runs offline. Validate it, then
    # run it to success (exit 0, "succeeded") through the public CLI — a real `caw run`,
    # proving the sample is runnable (the SAME task fanned to two branches, joined by a
    # synthesize node), not merely well-formed. cwd is a tmp dir so `.caw/runs` lands
    # there, leaving the shipped bundle untouched.
    monkeypatch.chdir(tmp_path)

    validated = runner.invoke(app, ["validate", str(_MOCK_SAMPLE)])
    assert validated.exit_code == 0, validated.output

    ran = runner.invoke(app, ["run", str(_MOCK_SAMPLE)])
    assert ran.exit_code == 0, ran.output
    assert "succeeded" in ran.output


def test_mock_sample_graph_fans_the_same_task_into_one_synthesis_node(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # AC1: the sample fans the SAME task out to two branches in parallel and a
    # synthesize node joins them. The plan shows two independent branches (no needs)
    # and a synthesize node that needs BOTH — the fan-out-synthesis shape.
    monkeypatch.chdir(tmp_path)

    plan = json.loads(runner.invoke(app, ["graph", str(_MOCK_SAMPLE), "--format", "json"]).output)
    by_id = {node["id"]: node for node in plan["nodes"]}
    branches = [node["id"] for node in plan["nodes"] if not node["needs"]]
    assert sorted(branches) == sorted(_BRANCH_IDS), "two independent fan-out branches"
    synth = by_id[_SYNTH_ID]
    assert sorted(synth["needs"]) == sorted(_BRANCH_IDS), (
        "the synthesize node fans in BOTH branches"
    )


def test_mock_sample_report_separates_conclusion_from_trace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # AC4: the sample's Markdown report separates the final conclusion (each node's
    # outcome, including the synthesize node's) from the trace evidence (the event
    # sequence). Run the sample offline, then render its Markdown report and assert the
    # conclusion section (## Nodes) precedes a distinct ## Trace section, with the
    # synthesize node's outcome living in the conclusion, not the trace.
    monkeypatch.chdir(tmp_path)

    ran = runner.invoke(app, ["run", str(_MOCK_SAMPLE)])
    assert ran.exit_code == 0, ran.output
    run_id = next(line.split()[1] for line in ran.output.splitlines() if line.startswith("run "))

    report = runner.invoke(app, ["report", run_id, "--format", "markdown"])
    assert report.exit_code == 0, report.output
    markdown = report.output

    nodes_heading = markdown.index("## Nodes")
    trace_heading = markdown.index("## Trace")
    # The conclusion comes first and the trace is a distinct, later section.
    assert nodes_heading < trace_heading, "the conclusion precedes the trace evidence"
    # The synthesize node's outcome lives in the conclusion, not the trace.
    conclusion = markdown[nodes_heading:trace_heading]
    assert f"{_SYNTH_ID} — succeeded" in conclusion, (
        "the synthesize node's conclusion is surfaced separately from the trace"
    )
