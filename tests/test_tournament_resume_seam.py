"""Group-level resume of a tournament Run Group (#17, ADR 0002 / 0009).

An interrupted tournament resumes at the GROUP level without re-running completed
rounds: the Run Group is the resumption unit. A SUCCEEDED round Run is never re-run
(Resume Eligibility, CONTEXT.md); the tournament continues from the persisted round
index, promoting the last completed round's winner forward. All offline.
"""

import json
from pathlib import Path

import pytest

from caw.controller import (
    ControllerError,
    TournamentSpec,
    load_group_state,
    resume_tournament,
    run_tournament,
)
from caw.runlayout import group_iterations_root, group_state_path
from caw.state import StateStore


def _write_fixture(
    path: Path, *, winner: str, scores: dict[str, int], next_fixture: str | None = None
) -> None:
    structured: dict[str, object] = {"winner": winner, "scores": scores}
    if next_fixture is not None:
        structured["next_fixture"] = next_fixture
    path.write_text(
        json.dumps({"exit_status": 0, "structured_output": structured}), encoding="utf-8"
    )


def _write_workflow(directory: Path, first_fixture: str) -> Path:
    workflow = directory / "round.yaml"
    workflow.write_text(
        "name: tournament-round\n"
        "version: 1\n"
        "nodes:\n"
        "  - id: compare\n"
        "    kind: agent\n"
        "    inputs:\n"
        "      adapter: mock\n"
        "      prompt: Compare the candidates.\n"
        f"      fixture: {first_fixture}\n",
        encoding="utf-8",
    )
    return workflow


def _spec(workflow: Path, *, rounds: int) -> TournamentSpec:
    return TournamentSpec.model_validate(
        {
            "workflow": str(workflow),
            "rounds": rounds,
            "compare_node": "compare",
            "winner_field": "winner",
            "promote": {"to_node": "compare", "to_field": "prompt"},
            "feedback": {
                "to_node": "compare",
                "to_field": "fixture",
                "from_field": "next_fixture",
            },
        }
    )


def _interrupt_after_round(group_id: str, base: Path, new_rounds: int) -> None:
    state_path = group_state_path(group_id, base)
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    persisted["spec"]["rounds"] = new_rounds
    persisted["status"] = "running"
    state_path.write_text(json.dumps(persisted, indent=2) + "\n", encoding="utf-8")


@pytest.mark.asyncio
async def test_resume_does_not_rerun_a_completed_round(tmp_path: Path) -> None:
    _write_fixture(
        tmp_path / "round1.fixture.json",
        winner="candidate-a",
        scores={"candidate-a": 9},
        next_fixture="round2.fixture.json",
    )
    _write_fixture(
        tmp_path / "round2.fixture.json", winner="candidate-c", scores={"candidate-c": 8}
    )
    workflow = _write_workflow(tmp_path, "round1.fixture.json")

    first = await run_tournament(_spec(workflow, rounds=1), base=tmp_path)
    assert first.status == "complete", "the single-round pass completed"
    assert len(first.iterations) == 1
    round1_run_id = first.iterations[0].run_id

    iterations_root = group_iterations_root(first.group_id, tmp_path)
    round1_dir = iterations_root / round1_run_id
    with StateStore(round1_dir / "state.sqlite", read_only=True) as state:
        attempts_before = state.max_attempt_per_node(round1_run_id)

    _interrupt_after_round(first.group_id, tmp_path, new_rounds=2)
    resumed = await resume_tournament(first.group_id, base=tmp_path)

    assert resumed.status == "complete", "the resumed tournament ran round 2"
    assert len(resumed.iterations) == 2
    assert resumed.iterations[0].run_id == round1_run_id, "round 1 keeps its run id"
    assert resumed.winner == "candidate-c", "the final round's winner is the result"

    with StateStore(round1_dir / "state.sqlite", read_only=True) as state:
        attempts_after = state.max_attempt_per_node(round1_run_id)
    assert attempts_after == attempts_before, "round 1's completed Run was not re-attempted"

    # Round 2's frozen snapshot carries round 1's promoted winner.
    second_run_dir = iterations_root / resumed.iterations[1].run_id
    snapshot = json.loads((second_run_dir / "workflow.normalized.json").read_text())
    compare = next(n for n in snapshot["workflow"]["nodes"] if n["id"] == "compare")
    assert compare["inputs"]["prompt"] == "candidate-a"


@pytest.mark.asyncio
async def test_resuming_a_complete_tournament_is_refused(tmp_path: Path) -> None:
    _write_fixture(
        tmp_path / "round.fixture.json", winner="candidate-a", scores={"candidate-a": 9}
    )
    workflow = _write_workflow(tmp_path, "round.fixture.json")

    result = await run_tournament(_spec(workflow, rounds=1), base=tmp_path)
    assert result.status == "complete"

    with pytest.raises(ControllerError, match="already finished"):
        await resume_tournament(result.group_id, base=tmp_path)


@pytest.mark.asyncio
async def test_resuming_a_failed_tournament_reruns_the_failed_round_and_continues(
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "round.fixture.json"
    fixture.write_text(json.dumps({"exit_status": 1, "stderr": "boom"}), encoding="utf-8")
    workflow = _write_workflow(tmp_path, "round.fixture.json")

    first = await run_tournament(_spec(workflow, rounds=2), base=tmp_path)
    assert first.status == "failed"
    failed_run_id = first.iterations[0].run_id

    # The failure was transient: rewrite to a succeeding round, then resume. The
    # re-run round succeeds in place and the tournament continues to round 2.
    _write_fixture(fixture, winner="candidate-a", scores={"candidate-a": 9})
    resumed = await resume_tournament(first.group_id, base=tmp_path)

    assert resumed.status == "complete", "the re-run round succeeds and the tournament finishes"
    assert resumed.iterations[0].run_id == failed_run_id, "the failed round resumes in place"

    persisted = load_group_state(first.group_id, tmp_path)
    assert persisted["status"] == "complete"
    assert persisted["winner"] == "candidate-a"
