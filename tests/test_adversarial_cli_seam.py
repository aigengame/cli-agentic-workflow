"""CLI seam for the `caw verify` adversarial-verification surface (#17, ADR 0009).

`caw verify` is a separate sub-typer (mirroring `caw loop`), leaving the single-run
and loop surfaces untouched. These tests drive the public CLI end to end offline with
the mock Adapter: a controller spec runs a verification to accepted, resumes a group,
reports a group as one aggregate, and scaffolds a runnable example. The exit-code
contract mirrors the single-run CLI (0 done/accepted/rejected, 1 failed, 2 config).
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
                "stdout": "REJECT",
                "structured_output": {"next_fixture": "round2.fixture.json"},
            }
        ),
        encoding="utf-8",
    )
    (directory / "round2.fixture.json").write_text(
        json.dumps({"exit_status": 0, "stdout": "ACCEPT", "structured_output": {}}),
        encoding="utf-8",
    )
    (directory / "round.yaml").write_text(
        "name: adversarial-round\nversion: 1\nnodes:\n"
        "  - id: verify\n    kind: agent\n    inputs:\n"
        "      adapter: mock\n      prompt: Verify the result.\n"
        "      fixture: round1.fixture.json\n",
        encoding="utf-8",
    )
    spec = directory / "verify.yaml"
    spec.write_text(
        "workflow: round.yaml\nmax_rounds: 5\nverify_node: verify\n"
        "accept:\n  ref:\n    node: verify\n    field: stdout\n  op: contains\n  value: ACCEPT\n"
        "feedback:\n  to_node: verify\n  to_field: fixture\n  from_field: next_fixture\n",
        encoding="utf-8",
    )
    return spec


def test_verify_run_drives_a_group_to_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _write_bundle(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["verify", "run", str(spec)])

    assert result.exit_code == 0, result.output
    assert "accepted" in result.output
    assert "group-" in result.output
    groups = list((tmp_path / ".caw" / "groups").iterdir())
    assert len(groups) == 1
    assert len(list((groups[0] / "iterations").iterdir())) == 2


def test_verify_report_aggregates_the_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _write_bundle(tmp_path)
    monkeypatch.chdir(tmp_path)

    run_result = runner.invoke(app, ["verify", "run", str(spec)])
    assert run_result.exit_code == 0, run_result.output
    group_id = _group_id(run_result.output)

    report_result = runner.invoke(app, ["verify", "report", group_id])
    assert report_result.exit_code == 0, report_result.output
    report = json.loads(report_result.output)
    assert report["group_id"] == group_id
    assert report["status"] == "accepted"
    assert len(report["iterations"]) == 2


def test_verify_run_with_a_failing_round_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "fail.fixture.json").write_text(
        json.dumps({"exit_status": 1, "stderr": "boom"}), encoding="utf-8"
    )
    (tmp_path / "round.yaml").write_text(
        "name: adversarial-round\nversion: 1\nnodes:\n"
        "  - id: verify\n    kind: agent\n    inputs:\n"
        "      adapter: mock\n      prompt: go\n      fixture: fail.fixture.json\n",
        encoding="utf-8",
    )
    (tmp_path / "verify.yaml").write_text(
        "workflow: round.yaml\nmax_rounds: 3\nverify_node: verify\n"
        "accept:\n  ref:\n    node: verify\n    field: stdout\n  op: contains\n  value: ACCEPT\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["verify", "run", str(tmp_path / "verify.yaml")])

    assert result.exit_code == 1, result.output
    assert "failed" in result.output


def test_verify_run_with_an_invalid_spec_is_a_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad = tmp_path / "verify.yaml"
    bad.write_text("max_rounds: 3\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["verify", "run", str(bad)])

    assert result.exit_code == 2
    lines = [line for line in result.output.splitlines() if line.strip()]
    assert lines[0].startswith("error:")


def test_verify_init_scaffolds_a_runnable_example(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # AC3: adversarial verification ships at least one example. `caw verify init`
    # scaffolds a COMPLETE, runnable bundle; running it drives to accepted offline.
    monkeypatch.chdir(tmp_path)

    scaffolded = runner.invoke(app, ["verify", "init"])
    assert scaffolded.exit_code == 0, scaffolded.output

    spec = tmp_path / "verify.yaml"
    assert spec.is_file()

    ran = runner.invoke(app, ["verify", "run", str(spec)])
    assert ran.exit_code == 0, ran.output
    assert "accepted" in ran.output


def test_verify_resume_continues_a_group(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spec = _write_bundle(tmp_path)
    spec.write_text(spec.read_text().replace("max_rounds: 5", "max_rounds: 1"))
    monkeypatch.chdir(tmp_path)

    first = runner.invoke(app, ["verify", "run", str(spec)])
    assert first.exit_code == 0, first.output
    assert "rejected" in first.output
    group_id = _group_id(first.output)

    state_path = tmp_path / ".caw" / "groups" / group_id / "group.json"
    persisted = json.loads(state_path.read_text())
    persisted["spec"]["max_rounds"] = 5
    persisted["status"] = "running"
    state_path.write_text(json.dumps(persisted, indent=2) + "\n")

    resumed = runner.invoke(app, ["verify", "resume", group_id])
    assert resumed.exit_code == 0, resumed.output
    assert "accepted" in resumed.output
