"""Group-level resume of a loop-until-done Run Group (#15, AC5, ADR 0002 / 0009).

An interrupted loop resumes at the GROUP level without re-running completed
iterations: the Run Group is the resumption unit. A SUCCEEDED iteration Run is
never re-run (Resume Eligibility, CONTEXT.md); the loop continues from the
persisted iteration index, feeding the last completed iteration's output forward.
All offline with the mock Adapter.
"""

import json
from pathlib import Path

import pytest

from caw.controller import (
    ControllerSpec,
    load_group_state,
    resume_loop_until_done,
    run_loop_until_done,
)
from caw.runlayout import group_iterations_root, group_state_path
from caw.state import StateStore


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


def _spec(workflow: Path, *, max_iterations: int) -> ControllerSpec:
    return ControllerSpec.model_validate(
        {
            "workflow": str(workflow),
            "max_iterations": max_iterations,
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


def _interrupt_after_iteration(group_id: str, base: Path, new_max: int) -> None:
    """Simulate an interruption: iteration 1 completed (not done) then the loop died.

    A real interruption between iterations persists ``group.json`` with the in-progress
    ``running`` marker (the loop hadn't reached the cap or done). This rewrites the
    capped-pass ``exhausted`` status to ``running`` AND raises ``max_iterations`` so a
    resume treats the group as interrupted and continues past the completed iteration —
    exactly the "interrupted before the next iteration ran" shape, without killing a
    process mid-run in a unit test.
    """
    state_path = group_state_path(group_id, base)
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    persisted["spec"]["max_iterations"] = new_max
    persisted["status"] = "running"
    state_path.write_text(json.dumps(persisted, indent=2) + "\n", encoding="utf-8")


@pytest.mark.asyncio
async def test_group_resume_does_not_rerun_a_completed_iteration(tmp_path: Path) -> None:
    # Run with max_iterations=1: iteration 1 completes NOT done, so the group stops
    # exhausted with one completed iteration — the interruption point. Then resume
    # the group: iteration 1's Run is NOT re-run, and the loop continues to
    # iteration 2 (which reports done), stopping the group.
    _write_fixture(tmp_path / "iter1.fixture.json", done=False, next_fixture="iter2.fixture.json")
    _write_fixture(tmp_path / "iter2.fixture.json", done=True)
    workflow = _write_workflow(tmp_path, "iter1.fixture.json")

    first = await run_loop_until_done(_spec(workflow, max_iterations=1), base=tmp_path)
    assert first.status == "exhausted", "the capped first pass stopped after one iteration"
    assert len(first.iterations) == 1
    iteration1_run_id = first.iterations[0].run_id

    iterations_root = group_iterations_root(first.group_id, tmp_path)
    iteration1_dir = iterations_root / iteration1_run_id
    # Pin iteration 1's persisted attempt count: a re-run would add an Attempt.
    with StateStore(iteration1_dir / "state.sqlite", read_only=True) as state:
        attempts_before = state.max_attempt_per_node(iteration1_run_id)
    snapshot_before = (iteration1_dir / "workflow.normalized.json").read_text()

    # Interruption: the loop died after iteration 1; mark it RUNNING, reopen the cap,
    # and resume.
    _interrupt_after_iteration(first.group_id, tmp_path, new_max=5)
    resumed = await resume_loop_until_done(first.group_id, base=tmp_path)

    assert resumed.status == "done", "the resumed loop ran iteration 2 and stopped on done"
    assert len(resumed.iterations) == 2, "iteration 2 was materialized on resume"
    assert resumed.iterations[0].run_id == iteration1_run_id, (
        "iteration 1 keeps its run id — the Run Group reuses, not re-creates, it"
    )

    # Iteration 1 was NOT re-run: its Attempts and frozen snapshot are unchanged.
    with StateStore(iteration1_dir / "state.sqlite", read_only=True) as state:
        attempts_after = state.max_attempt_per_node(iteration1_run_id)
    assert attempts_after == attempts_before, "iteration 1's completed Run was not re-attempted"
    assert (iteration1_dir / "workflow.normalized.json").read_text() == snapshot_before

    # Iteration 2 is a distinct new run directory.
    iteration2_run_id = resumed.iterations[1].run_id
    assert iteration2_run_id != iteration1_run_id
    assert (iterations_root / iteration2_run_id / "state.sqlite").is_file()


@pytest.mark.asyncio
async def test_resuming_a_finished_done_group_is_refused(tmp_path: Path) -> None:
    # A group that already reached done has nothing to resume — refused as a
    # controller-class error (mirroring single-run Resume Eligibility).
    from caw.controller import ControllerError

    _write_fixture(tmp_path / "done.fixture.json", done=True)
    workflow = _write_workflow(tmp_path, "done.fixture.json")

    result = await run_loop_until_done(_spec(workflow, max_iterations=3), base=tmp_path)
    assert result.status == "done"

    with pytest.raises(ControllerError, match="already finished"):
        await resume_loop_until_done(result.group_id, base=tmp_path)


@pytest.mark.asyncio
async def test_resuming_an_exhausted_group_is_refused(tmp_path: Path) -> None:
    # A group that hit its cap (exhausted) is TERMINAL: there is nothing left to do,
    # so resume refuses it with a clear error, exactly like a done group — both are
    # terminal stop reasons, distinct from the in-progress `running` marker.
    from caw.controller import ControllerError

    _write_fixture(tmp_path / "loop.fixture.json", done=False, next_fixture="loop.fixture.json")
    workflow = _write_workflow(tmp_path, "loop.fixture.json")

    result = await run_loop_until_done(_spec(workflow, max_iterations=2), base=tmp_path)
    assert result.status == "exhausted", "the loop hit the cap without reaching done"

    with pytest.raises(ControllerError, match="already finished"):
        await resume_loop_until_done(result.group_id, base=tmp_path)


@pytest.mark.asyncio
async def test_resuming_a_failed_group_reruns_the_failed_iteration_and_continues(
    tmp_path: Path,
) -> None:
    # A group whose last iteration FAILED is RESUMABLE (Resume Eligibility): the failed
    # last Run is `resume_run`'d in place, and once it succeeds the loop continues. Here
    # the iteration's fixture fails first (non-zero exit), then is rewritten to a
    # succeeding done fixture before resume — so the in-place re-run reaches done and the
    # group stops, reusing the same iteration run id (not a fresh iteration).
    fixture = tmp_path / "iter.fixture.json"
    fixture.write_text(json.dumps({"exit_status": 1, "stderr": "boom"}), encoding="utf-8")
    workflow = _write_workflow(tmp_path, "iter.fixture.json")

    first = await run_loop_until_done(_spec(workflow, max_iterations=3), base=tmp_path)
    assert first.status == "failed", "the first pass stopped on the failed iteration"
    assert len(first.iterations) == 1
    failed_run_id = first.iterations[0].run_id
    assert not first.iterations[0].succeeded

    # The failure was transient: rewrite the fixture to succeed-and-report-done, then
    # resume. `resume_run` re-runs the failed node in place under the SAME run id.
    _write_fixture(fixture, done=True)
    resumed = await resume_loop_until_done(first.group_id, base=tmp_path)

    assert resumed.status == "done", "the re-run iteration now succeeds and reports done"
    assert resumed.iterations[0].run_id == failed_run_id, (
        "the failed iteration is resumed in place — same run id, not a fresh iteration"
    )
    assert resumed.iterations[0].succeeded, "the resumed iteration's Run now succeeded"

    # group.json records the terminal done status after the resume.
    persisted = load_group_state(first.group_id, tmp_path)
    assert persisted["status"] == "done"


@pytest.mark.asyncio
async def test_group_state_is_authoritative_for_resume(tmp_path: Path) -> None:
    # group.json is the authoritative controller state the resume reads: it records
    # the ordered iterations and the spec, so resume reconstructs the loop from it.
    _write_fixture(tmp_path / "iter1.fixture.json", done=False, next_fixture="iter2.fixture.json")
    _write_fixture(tmp_path / "iter2.fixture.json", done=True)
    workflow = _write_workflow(tmp_path, "iter1.fixture.json")

    first = await run_loop_until_done(_spec(workflow, max_iterations=1), base=tmp_path)
    persisted = load_group_state(first.group_id, tmp_path)

    assert persisted["spec"]["evaluate_node"] == "verdict"
    assert [it["run_id"] for it in persisted["iterations"]] == [first.iterations[0].run_id]
