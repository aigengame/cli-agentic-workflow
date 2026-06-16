"""CLI-seam tests for the fan-out-synthesis sample bundle (#14).

The fan-out-synthesis sample fans the SAME task out to two agent branches in
parallel and joins their results in a synthesis node (CONTEXT.md: Parallel; the
issue's hand-authored fan-out-synthesis shape, built on the existing `parallel`
expander). ``caw fanout init`` scaffolds the COMPLETE bundle — the `parallel`
workflow plus the per-branch and synthesis fixtures — so it validates and runs
to success offline with the mock Adapter, no real Agent CLI and no tokens.

The bundle is run end-to-end through `caw run` (a real CLI-seam run, not just
validate), and its Markdown report is asserted to separate the final conclusion
(node statuses + the synthesis node's output) from the trace evidence (events) —
the issue's AC4. Success is proven through the public CLI, never by inspecting
internals (project testing philosophy).
"""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from caw.cli import app

runner = CliRunner()

_SYNTH_ID = "synthesize"


def test_fanout_init_scaffolds_a_sample_that_validates_and_runs_to_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # AC1+AC2: `caw fanout init` scaffolds a COMPLETE fan-out-synthesis sample whose
    # mock-Adapter variant runs offline. Scaffold it, then validate AND run it offline —
    # a real `caw run` to success (exit 0, succeeded), proving the sample is runnable
    # (the same task fanned to two branches, joined by a synthesis node), not merely
    # well-formed.
    monkeypatch.chdir(tmp_path)

    scaffolded = runner.invoke(app, ["fanout", "init"])
    assert scaffolded.exit_code == 0, scaffolded.output

    sample = tmp_path / "fanout.yaml"
    assert sample.is_file(), "fanout init writes the sample workflow by default"
    assert "fanout.yaml" in scaffolded.output, "it names the file it created"

    validated = runner.invoke(app, ["validate", str(sample)])
    assert validated.exit_code == 0, validated.output

    ran = runner.invoke(app, ["run", str(sample)])
    assert ran.exit_code == 0, ran.output
    assert "succeeded" in ran.output


def test_fanout_sample_graph_fans_two_branches_into_one_synthesis_node(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # AC1: the sample fans the SAME task out to two branches in parallel and a
    # synthesis node joins them. The expanded plan shows two independent branches
    # (no needs) and a synthesis node that needs BOTH — the fan-out-synthesis shape.
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["fanout", "init"]).exit_code == 0
    sample = tmp_path / "fanout.yaml"

    import json

    plan = json.loads(runner.invoke(app, ["graph", str(sample), "--format", "json"]).output)
    branches = [node["id"] for node in plan["nodes"] if not node["needs"]]
    assert len(branches) == 2, "two independent fan-out branches"
    synth = next(node for node in plan["nodes"] if node["id"] == _SYNTH_ID)
    assert sorted(synth["needs"]) == sorted(branches), "the synthesis node fans in both branches"


def test_fanout_report_separates_conclusion_from_trace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # AC4: the sample's Markdown report separates the final conclusion (the synthesis
    # node's outcome) from the trace evidence (the event sequence). Run the sample
    # offline, then render its Markdown report and assert the conclusion sections
    # (Nodes) and the Trace section are distinct headings, with the synthesis node in
    # the conclusion.
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["fanout", "init"]).exit_code == 0
    sample = tmp_path / "fanout.yaml"

    ran = runner.invoke(app, ["run", str(sample)])
    assert ran.exit_code == 0, ran.output
    run_id = next(line.split()[1] for line in ran.output.splitlines() if line.startswith("run "))

    report = runner.invoke(app, ["report", run_id, "--format", "markdown"])
    assert report.exit_code == 0, report.output
    markdown = report.output

    nodes_heading = markdown.index("## Nodes")
    trace_heading = markdown.index("## Trace")
    # The conclusion comes first and the trace is a distinct, later section.
    assert nodes_heading < trace_heading, "the conclusion precedes the trace evidence"
    # The synthesis node's outcome lives in the conclusion, not the trace.
    conclusion = markdown[nodes_heading:trace_heading]
    assert f"{_SYNTH_ID} — succeeded" in conclusion, (
        "the synthesis node's conclusion is surfaced separately from the trace"
    )


def test_fanout_init_to_an_explicit_path_writes_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "my-fanout.yaml"

    result = runner.invoke(app, ["fanout", "init", str(target)])

    assert result.exit_code == 0, result.output
    assert target.is_file(), "fanout init writes to the explicit path"


def test_fanout_init_refuses_to_overwrite_an_existing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Scaffolding must never silently clobber an author's file: an existing target is
    # a config-class refusal (exit 2, one `error:` line), at parity with the other
    # scaffold commands.
    monkeypatch.chdir(tmp_path)
    existing = tmp_path / "fanout.yaml"
    existing.write_text("name: mine\n", encoding="utf-8")

    result = runner.invoke(app, ["fanout", "init"])

    assert result.exit_code == 2
    assert existing.read_text(encoding="utf-8") == "name: mine\n", "the file is untouched"
