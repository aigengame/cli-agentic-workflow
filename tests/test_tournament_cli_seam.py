"""CLI seam for the `caw tournament` surface (#17, ADR 0009).

`caw tournament` is a separate sub-typer (mirroring `caw loop`). These tests drive
the public CLI end to end offline with the mock Adapter: a controller spec runs a
tournament to complete, names the final winner, reports the group with each round's
comparison evidence, resumes a group, and scaffolds a runnable example.
"""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from caw.cli import app

runner = CliRunner()


def _group_id(output: str) -> str:
    for line in output.splitlines():
        for token in line.split():
            if token.startswith("group-"):
                return token.rstrip(":")
    raise AssertionError(f"no group id in output: {output!r}")


def _write_bundle(directory: Path) -> Path:
    (directory / "round1.fixture.json").write_text(
        json.dumps(
            {
                "exit_status": 0,
                "structured_output": {
                    "winner": "candidate-a",
                    "scores": {"candidate-a": 9, "candidate-b": 4},
                    "next_fixture": "round2.fixture.json",
                },
            }
        ),
        encoding="utf-8",
    )
    (directory / "round2.fixture.json").write_text(
        json.dumps(
            {
                "exit_status": 0,
                "structured_output": {
                    "winner": "candidate-c",
                    "scores": {"candidate-a": 6, "candidate-c": 8},
                },
            }
        ),
        encoding="utf-8",
    )
    (directory / "round.yaml").write_text(
        "name: tournament-round\nversion: 1\nnodes:\n"
        "  - id: compare\n    kind: agent\n    inputs:\n"
        "      adapter: mock\n      prompt: Compare the candidates.\n"
        "      fixture: round1.fixture.json\n",
        encoding="utf-8",
    )
    spec = directory / "tournament.yaml"
    spec.write_text(
        "workflow: round.yaml\nrounds: 2\ncompare_node: compare\nwinner_field: winner\n"
        "promote:\n  to_node: compare\n  to_field: prompt\n"
        "feedback:\n  to_node: compare\n  to_field: fixture\n  from_field: next_fixture\n",
        encoding="utf-8",
    )
    return spec


def test_tournament_run_completes_and_names_the_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _write_bundle(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["tournament", "run", str(spec)])

    assert result.exit_code == 0, result.output
    assert "complete" in result.output
    assert "candidate-c" in result.output, "the final winner is named in the output"
    groups = list((tmp_path / ".caw" / "groups").iterdir())
    assert len(list((groups[0] / "iterations").iterdir())) == 2


def test_tournament_report_carries_comparison_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _write_bundle(tmp_path)
    monkeypatch.chdir(tmp_path)

    run_result = runner.invoke(app, ["tournament", "run", str(spec)])
    assert run_result.exit_code == 0, run_result.output
    group_id = _group_id(run_result.output)

    report_result = runner.invoke(app, ["tournament", "report", group_id])
    assert report_result.exit_code == 0, report_result.output
    report = json.loads(report_result.output)
    assert report["status"] == "complete"
    assert report["winner"] == "candidate-c", "the group report carries the final winner"
    assert len(report["iterations"]) == 2
    for iteration in report["iterations"]:
        compare = next(node for node in iteration["nodes"] if node["id"] == "compare")
        assert "winner" in compare["structured_output"]
        assert "scores" in compare["structured_output"]


def test_tournament_run_with_a_failing_round_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "fail.fixture.json").write_text(
        json.dumps({"exit_status": 1, "stderr": "boom"}), encoding="utf-8"
    )
    (tmp_path / "round.yaml").write_text(
        "name: tournament-round\nversion: 1\nnodes:\n"
        "  - id: compare\n    kind: agent\n    inputs:\n"
        "      adapter: mock\n      prompt: go\n      fixture: fail.fixture.json\n",
        encoding="utf-8",
    )
    (tmp_path / "tournament.yaml").write_text(
        "workflow: round.yaml\nrounds: 3\ncompare_node: compare\nwinner_field: winner\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["tournament", "run", str(tmp_path / "tournament.yaml")])

    assert result.exit_code == 1, result.output
    assert "failed" in result.output


def test_tournament_run_with_an_invalid_spec_is_a_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad = tmp_path / "tournament.yaml"
    bad.write_text("rounds: 0\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["tournament", "run", str(bad)])

    assert result.exit_code == 2
    lines = [line for line in result.output.splitlines() if line.strip()]
    assert lines[0].startswith("error:")


def test_tournament_init_scaffolds_a_runnable_example(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # AC3: the tournament ships at least one example. `caw tournament init` scaffolds
    # a COMPLETE, runnable bundle; running it completes the tournament offline.
    monkeypatch.chdir(tmp_path)

    scaffolded = runner.invoke(app, ["tournament", "init"])
    assert scaffolded.exit_code == 0, scaffolded.output

    spec = tmp_path / "tournament.yaml"
    assert spec.is_file()

    ran = runner.invoke(app, ["tournament", "run", str(spec)])
    assert ran.exit_code == 0, ran.output
    assert "complete" in ran.output


def test_tournament_resume_continues_a_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _write_bundle(tmp_path)
    spec.write_text(spec.read_text().replace("rounds: 2", "rounds: 1"))
    monkeypatch.chdir(tmp_path)

    first = runner.invoke(app, ["tournament", "run", str(spec)])
    assert first.exit_code == 0, first.output
    assert "complete" in first.output
    group_id = _group_id(first.output)

    state_path = tmp_path / ".caw" / "groups" / group_id / "group.json"
    persisted = json.loads(state_path.read_text())
    persisted["spec"]["rounds"] = 2
    persisted["status"] = "running"
    state_path.write_text(json.dumps(persisted, indent=2) + "\n")

    resumed = runner.invoke(app, ["tournament", "resume", group_id])
    assert resumed.exit_code == 0, resumed.output
    assert "complete" in resumed.output
    report = json.loads(runner.invoke(app, ["tournament", "report", group_id]).output)
    assert len(report["iterations"]) == 2
