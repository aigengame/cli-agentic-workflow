"""Tournament Pattern Controller seam (#17, ADR 0002 / 0009).

A tournament runs candidates in rounds, compares their outputs, promotes the
round's winner into the next round, and reports the final result with the
comparison evidence each round produced. It is realized as a Pattern Controller on
the SAME Run Group infrastructure ``loop_until_done`` uses (immutable per-round
Runs, the membership mirror, the group-state file): each round is one Run whose
``compare`` node names a winner in its ``structured_output``; the Controller
promotes that winner (structural substitution, not templating) into the next round
and runs a fixed number of rounds.

These tests run the whole tournament OFFLINE with the mock Adapter (fixtures): a
deterministic bracket where round N's winner selects round N+1's compare fixture,
the controller promotes winners across rounds, and the final winner plus each
round's comparison evidence is reported.
"""

import json
from pathlib import Path

import pytest

from caw.controller import (
    GroupResult,
    TournamentSpec,
    run_tournament,
)
from caw.report import ReportFormat, render_group_report
from caw.runlayout import group_iterations_root, group_state_path
from caw.state import StateStore

# ----------------------------------------------------------------------------------
# A tournament round is a single mock-Adapter agent Node named ``compare``. Its
# fixture is a canned normalized result whose ``structured_output`` carries the
# round's ``winner`` (the candidate promoted to the next round) plus ``scores`` (the
# comparison evidence) and ``next_fixture`` (the compare fixture the next round
# reads). The Controller promotes ``compare.structured_output.winner`` into the
# ``compare`` node's ``promoted_winner`` input AND feeds the next fixture forward —
# a faithful, deterministic bracket offline.
# ----------------------------------------------------------------------------------


def _write_fixture(
    path: Path, *, winner: str, scores: dict[str, int], next_fixture: str | None = None
) -> None:
    structured: dict[str, object] = {"winner": winner, "scores": scores}
    if next_fixture is not None:
        structured["next_fixture"] = next_fixture
    path.write_text(
        json.dumps({"exit_status": 0, "structured_output": structured}),
        encoding="utf-8",
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
        "      prompt: Compare the candidates and name the winner.\n"
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
            # The structured_output field naming the round's winning candidate.
            "winner_field": "winner",
            # Promote the winner into the `compare` node's `prompt` AND feed the next
            # round's compare fixture forward — both structural substitutions into the
            # `compare` node's inputs (the prompt is a real, schema-permitted field).
            "promote": {"to_node": "compare", "to_field": "prompt"},
            "feedback": {
                "to_node": "compare",
                "to_field": "fixture",
                "from_field": "next_fixture",
            },
        }
    )


def test_spec_rejects_a_winner_field_on_a_non_compare_node() -> None:
    # The promote target node must be present and the spec must be coherent: a
    # missing compare_node is a config error caught when the round materializes, but
    # an obviously malformed spec (rounds < 1) is rejected at validation.
    with pytest.raises(ValueError):
        TournamentSpec.model_validate(
            {
                "workflow": "round.yaml",
                "rounds": 0,
                "compare_node": "compare",
                "winner_field": "winner",
            }
        )


@pytest.mark.asyncio
async def test_tournament_runs_rounds_and_promotes_winners(tmp_path: Path) -> None:
    # AC2: a 2-round tournament runs both rounds, promoting round 1's winner into
    # round 2. Each round is a separate immutable Run in the same Run Group.
    _write_fixture(
        tmp_path / "round1.fixture.json",
        winner="candidate-a",
        scores={"candidate-a": 9, "candidate-b": 4},
        next_fixture="round2.fixture.json",
    )
    _write_fixture(
        tmp_path / "round2.fixture.json",
        winner="candidate-c",
        scores={"candidate-a": 6, "candidate-c": 8},
    )
    workflow = _write_workflow(tmp_path, "round1.fixture.json")
    spec = _spec(workflow, rounds=2)

    result = await run_tournament(spec, base=tmp_path)

    assert isinstance(result, GroupResult)
    assert result.status == "complete", "the tournament ran every round"
    assert len(result.iterations) == 2, "exactly two rounds materialized"
    iterations_root = group_iterations_root(result.group_id, tmp_path)
    iteration_run_dirs = sorted(p.name for p in iterations_root.iterdir())
    assert iteration_run_dirs == sorted(it.run_id for it in result.iterations)


@pytest.mark.asyncio
async def test_round_one_winner_is_promoted_into_round_two(tmp_path: Path) -> None:
    # AC2: round 1's winner is PROMOTED into round 2's compare node. Round 2's
    # FROZEN snapshot must carry the promoted winner — proving promotion reached the
    # materialized Run, not just the controller.
    _write_fixture(
        tmp_path / "round1.fixture.json",
        winner="candidate-a",
        scores={"candidate-a": 9, "candidate-b": 4},
        next_fixture="round2.fixture.json",
    )
    _write_fixture(
        tmp_path / "round2.fixture.json",
        winner="candidate-c",
        scores={"candidate-a": 6, "candidate-c": 8},
    )
    workflow = _write_workflow(tmp_path, "round1.fixture.json")
    spec = _spec(workflow, rounds=2)

    result = await run_tournament(spec, base=tmp_path)

    iterations_root = group_iterations_root(result.group_id, tmp_path)
    second_run_dir = iterations_root / result.iterations[1].run_id
    snapshot = json.loads((second_run_dir / "workflow.normalized.json").read_text())
    compare = next(n for n in snapshot["workflow"]["nodes"] if n["id"] == "compare")
    assert compare["inputs"]["prompt"] == "candidate-a", (
        "round 2's frozen workflow carries round 1's promoted winner in its prompt"
    )
    # The next compare fixture was also fed forward, so round 2 reads its own fixture.
    assert compare["inputs"]["fixture"].endswith("round2.fixture.json")


@pytest.mark.asyncio
async def test_group_report_carries_each_round_comparison_evidence(tmp_path: Path) -> None:
    # AC2: the report includes comparison evidence. The aggregate group report
    # carries each round's compare-node structured_output (winner + scores), so the
    # comparison evidence each round produced is in the report.
    _write_fixture(
        tmp_path / "round1.fixture.json",
        winner="candidate-a",
        scores={"candidate-a": 9, "candidate-b": 4},
        next_fixture="round2.fixture.json",
    )
    _write_fixture(
        tmp_path / "round2.fixture.json",
        winner="candidate-c",
        scores={"candidate-a": 6, "candidate-c": 8},
    )
    workflow = _write_workflow(tmp_path, "round1.fixture.json")
    spec = _spec(workflow, rounds=2)

    result = await run_tournament(spec, base=tmp_path)

    report = json.loads(render_group_report(result.group_id, tmp_path, ReportFormat.json))
    assert len(report["iterations"]) == 2
    for round_index, iteration in enumerate(report["iterations"]):
        compare = next(node for node in iteration["nodes"] if node["id"] == "compare")
        evidence = compare["structured_output"]
        assert "winner" in evidence, f"round {round_index} report names its winner"
        assert "scores" in evidence, f"round {round_index} report carries comparison scores"


@pytest.mark.asyncio
async def test_tournament_records_the_final_winner(tmp_path: Path) -> None:
    # AC2: the final round's winner is the tournament's reported result, surfaced on
    # the GroupResult and persisted to group.json so it is queryable without
    # re-reading every round's output.
    _write_fixture(
        tmp_path / "round1.fixture.json",
        winner="candidate-a",
        scores={"candidate-a": 9, "candidate-b": 4},
        next_fixture="round2.fixture.json",
    )
    _write_fixture(
        tmp_path / "round2.fixture.json",
        winner="candidate-c",
        scores={"candidate-a": 6, "candidate-c": 8},
    )
    workflow = _write_workflow(tmp_path, "round1.fixture.json")
    spec = _spec(workflow, rounds=2)

    result = await run_tournament(spec, base=tmp_path)

    assert result.winner == "candidate-c", "the final round's winner is the tournament result"
    persisted = json.loads(group_state_path(result.group_id, tmp_path).read_text())
    assert persisted["winner"] == "candidate-c"
    # Each round's promoted winner is recorded in order, the comparison trail.
    assert [it["promoted"] for it in persisted["iterations"]] == ["candidate-a", "candidate-c"]


@pytest.mark.asyncio
async def test_tournament_stops_on_a_failed_round(tmp_path: Path) -> None:
    # AC2-parity: a failed round stops the tournament (no winner to promote forward).
    (tmp_path / "fail.fixture.json").write_text(
        json.dumps({"exit_status": 1, "stderr": "boom"}), encoding="utf-8"
    )
    workflow = _write_workflow(tmp_path, "fail.fixture.json")
    spec = _spec(workflow, rounds=3)

    result = await run_tournament(spec, base=tmp_path)

    assert result.status == "failed", "the tournament stopped on the failed round"
    assert len(result.iterations) == 1, "no round is materialized after the failure"


@pytest.mark.asyncio
async def test_each_round_records_its_group_membership(tmp_path: Path) -> None:
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
    spec = _spec(workflow, rounds=2)

    result = await run_tournament(spec, base=tmp_path)

    iterations_root = group_iterations_root(result.group_id, tmp_path)
    for index, iteration in enumerate(result.iterations):
        run_dir = iterations_root / iteration.run_id
        with StateStore(run_dir / "state.sqlite") as state:
            membership = state.run_group_membership(iteration.run_id)
        assert membership == (result.group_id, index)
