"""Pattern Controller seam: loop-until-done over a Run Group (#15, ADR 0002 / 0009).

The Controller drives the EXISTING ``execute_run``/``resume_run`` as black boxes,
materializing each iteration as a separate immutable Run under a Run Group. These
tests run the whole loop OFFLINE with the mock Adapter (fixtures), so a deterministic
multi-iteration loop is proven with no tokens: a faithful feedback loop where
iteration N's structured output selects iteration N+1's fixture, the done Predicate
reads the iteration's verdict, and the loop stops on done / failure / max-iterations.
"""

import json
from pathlib import Path

import pytest

from caw.controller import ControllerSpec, GroupResult, run_loop_until_done
from caw.runlayout import group_iterations_root, group_state_path
from caw.state import StateStore

# ----------------------------------------------------------------------------------
# A loop iteration is a single mock-Adapter agent Node named ``verdict``. Its fixture
# is a canned normalized result whose ``stdout`` carries the stop verdict (the
# done Predicate reads it with ``contains``, the op valid on the textual ``stdout``
# field) and whose ``structured_output`` carries ``next_fixture`` (the fixture
# iteration N+1 should read). The Controller's feedback substitutes
# ``verdict.structured_output.next_fixture`` into the ``verdict`` node's ``fixture``
# field for the next iteration — a faithful, deterministic feedback loop offline.
# ----------------------------------------------------------------------------------


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
    # The done Predicate reuses the existing `when` algebra: the iteration is done
    # when the `verdict` node's textual `stdout` contains the FINISHED verdict
    # (`contains` is the op valid on the string `stdout` field, #7).
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


def test_spec_rejects_a_done_ref_that_misses_evaluate_node() -> None:
    # The done Predicate may only reference `evaluate_node`: at evaluation time only
    # that node's output is supplied to the Predicate, so a ref to a typo or a
    # different node would silently evaluate false and the loop would wrongly EXHAUST.
    # `ControllerSpec.model_validate` must reject it (fail fast over fail silent),
    # mirroring model.py's `when`-refs-must-be-in-`needs` invariant.
    with pytest.raises(ValueError, match="done predicate references node 'verdcit'"):
        ControllerSpec.model_validate(
            {
                "workflow": "iteration.yaml",
                "max_iterations": 5,
                "evaluate_node": "verdict",
                # `verdcit` is a typo — not the evaluate_node — so the done Predicate
                # would never see this node's output.
                "done": {
                    "ref": {"node": "verdcit", "field": "stdout"},
                    "op": "contains",
                    "value": "FINISHED",
                },
            }
        )


@pytest.mark.asyncio
async def test_loop_stops_at_the_iteration_that_reports_done(tmp_path: Path) -> None:
    # AC1/AC2/AC4: iteration 1 reports not-done and points to iter2.fixture.json;
    # the Controller feeds that forward; iteration 2 reads it and reports done, so
    # the loop stops at iteration 2 — each iteration a separate immutable Run.
    _write_fixture(tmp_path / "iter1.fixture.json", done=False, next_fixture="iter2.fixture.json")
    _write_fixture(tmp_path / "iter2.fixture.json", done=True)
    workflow = _write_workflow(tmp_path, "iter1.fixture.json")
    spec = _spec(workflow, max_iterations=5)

    result = await run_loop_until_done(spec, base=tmp_path)

    assert isinstance(result, GroupResult)
    assert result.status == "done", "the loop stopped because the done Predicate held"
    assert len(result.iterations) == 2, "exactly two iterations materialized"
    # Each iteration is a separate Run directory under the group's iterations root.
    iterations_root = group_iterations_root(result.group_id, tmp_path)
    iteration_run_dirs = sorted(p.name for p in iterations_root.iterdir())
    assert len(iteration_run_dirs) == 2
    assert iteration_run_dirs == sorted(it.run_id for it in result.iterations)


@pytest.mark.asyncio
async def test_loop_stops_at_max_iterations_when_never_done(tmp_path: Path) -> None:
    # AC4: a loop that never reports done stops at max_iterations and reports it,
    # rather than looping forever. Every iteration points forward to a not-done
    # fixture, so the done Predicate is never satisfied.
    _write_fixture(tmp_path / "loop.fixture.json", done=False, next_fixture="loop.fixture.json")
    workflow = _write_workflow(tmp_path, "loop.fixture.json")
    spec = _spec(workflow, max_iterations=3)

    result = await run_loop_until_done(spec, base=tmp_path)

    assert result.status == "exhausted", "the loop stopped at the iteration cap"
    assert len(result.iterations) == 3, "exactly max_iterations runs materialized"


@pytest.mark.asyncio
async def test_loop_stops_on_a_failed_iteration(tmp_path: Path) -> None:
    # AC4: a failed iteration stops the loop (no point feeding a failed result
    # forward). A non-zero fixture exit makes the `verdict` node fail.
    (tmp_path / "fail.fixture.json").write_text(
        json.dumps({"exit_status": 1, "stderr": "boom"}), encoding="utf-8"
    )
    workflow = _write_workflow(tmp_path, "fail.fixture.json")
    spec = _spec(workflow, max_iterations=5)

    result = await run_loop_until_done(spec, base=tmp_path)

    assert result.status == "failed", "the loop stopped on the failed iteration"
    assert len(result.iterations) == 1, "no iteration is materialized after the failure"


@pytest.mark.asyncio
async def test_each_run_records_its_group_id_and_iteration_index(tmp_path: Path) -> None:
    # AC3: each iteration's Run records the run group id and iteration index in its
    # own State, queryable from the run. Controller state is persisted in group.json.
    _write_fixture(tmp_path / "iter1.fixture.json", done=False, next_fixture="iter2.fixture.json")
    _write_fixture(tmp_path / "iter2.fixture.json", done=True)
    workflow = _write_workflow(tmp_path, "iter1.fixture.json")
    spec = _spec(workflow, max_iterations=5)

    result = await run_loop_until_done(spec, base=tmp_path)

    iterations_root = group_iterations_root(result.group_id, tmp_path)
    for index, iteration in enumerate(result.iterations):
        run_dir = iterations_root / iteration.run_id
        with StateStore(run_dir / "state.sqlite") as state:
            membership = state.run_group_membership(iteration.run_id)
        assert membership == (result.group_id, index), (
            f"iteration {index} records its group id and index in its own State"
        )

    # Controller state is persisted in group.json.
    persisted = json.loads(group_state_path(result.group_id, tmp_path).read_text())
    assert persisted["group_id"] == result.group_id
    assert persisted["iteration_index"] == len(result.iterations)
    assert persisted["status"] == "done"
    assert [it["run_id"] for it in persisted["iterations"]] == [
        it.run_id for it in result.iterations
    ]


@pytest.mark.asyncio
async def test_feedback_is_substituted_into_the_next_iteration_workflow(tmp_path: Path) -> None:
    # AC2: feedback from iteration N is passed as inputs to iteration N+1. The
    # second iteration's FROZEN snapshot must show the substituted fixture path —
    # proving feedback reached the materialized run, not just the controller.
    _write_fixture(tmp_path / "iter1.fixture.json", done=False, next_fixture="iter2.fixture.json")
    _write_fixture(tmp_path / "iter2.fixture.json", done=True)
    workflow = _write_workflow(tmp_path, "iter1.fixture.json")
    spec = _spec(workflow, max_iterations=5)

    result = await run_loop_until_done(spec, base=tmp_path)

    iterations_root = group_iterations_root(result.group_id, tmp_path)
    second_run_dir = iterations_root / result.iterations[1].run_id
    snapshot = json.loads((second_run_dir / "workflow.normalized.json").read_text())
    verdict = next(n for n in snapshot["workflow"]["nodes"] if n["id"] == "verdict")
    assert verdict["inputs"]["fixture"].endswith("iter2.fixture.json"), (
        "iteration 2's frozen workflow carries the fixture fed forward from iteration 1"
    )
