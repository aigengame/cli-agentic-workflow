"""Group-level resume of an adversarial-verification Run Group (#17, ADR 0002 / 0009).

An interrupted verification resumes at the GROUP level without re-running completed
rounds: the Run Group is the resumption unit. A SUCCEEDED round Run is never re-run
(Resume Eligibility, CONTEXT.md); the loop continues from the persisted round index,
feeding the last completed round's verifier feedback forward. All offline.
"""

import json
from pathlib import Path

import pytest

from caw.controller import (
    AdversarialSpec,
    ControllerError,
    load_group_state,
    resume_adversarial_verification,
    run_adversarial_verification,
)
from caw.runlayout import group_iterations_root, group_state_path
from caw.state import StateStore


def _write_fixture(path: Path, *, accept: bool, next_fixture: str | None = None) -> None:
    structured: dict[str, object] = {}
    if next_fixture is not None:
        structured["next_fixture"] = next_fixture
    path.write_text(
        json.dumps(
            {
                "exit_status": 0,
                "stdout": "ACCEPT" if accept else "REJECT",
                "structured_output": structured,
            }
        ),
        encoding="utf-8",
    )


def _write_workflow(directory: Path, first_fixture: str) -> Path:
    workflow = directory / "iteration.yaml"
    workflow.write_text(
        "name: adversarial-round\n"
        "version: 1\n"
        "nodes:\n"
        "  - id: verify\n"
        "    kind: agent\n"
        "    inputs:\n"
        "      adapter: mock\n"
        "      prompt: Verify the result.\n"
        f"      fixture: {first_fixture}\n",
        encoding="utf-8",
    )
    return workflow


def _spec(workflow: Path, *, max_rounds: int) -> AdversarialSpec:
    return AdversarialSpec.model_validate(
        {
            "workflow": str(workflow),
            "max_rounds": max_rounds,
            "verify_node": "verify",
            "accept": {
                "ref": {"node": "verify", "field": "stdout"},
                "op": "contains",
                "value": "ACCEPT",
            },
            "feedback": {
                "to_node": "verify",
                "to_field": "fixture",
                "from_field": "next_fixture",
            },
        }
    )


def _interrupt_after_round(group_id: str, base: Path, new_max: int) -> None:
    state_path = group_state_path(group_id, base)
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    persisted["spec"]["max_rounds"] = new_max
    persisted["status"] = "running"
    state_path.write_text(json.dumps(persisted, indent=2) + "\n", encoding="utf-8")


@pytest.mark.asyncio
async def test_resume_does_not_rerun_a_completed_round(tmp_path: Path) -> None:
    _write_fixture(
        tmp_path / "round1.fixture.json", accept=False, next_fixture="round2.fixture.json"
    )
    _write_fixture(tmp_path / "round2.fixture.json", accept=True)
    workflow = _write_workflow(tmp_path, "round1.fixture.json")

    first = await run_adversarial_verification(_spec(workflow, max_rounds=1), base=tmp_path)
    assert first.status == "rejected", "the capped first pass stopped after one round"
    assert len(first.iterations) == 1
    round1_run_id = first.iterations[0].run_id

    iterations_root = group_iterations_root(first.group_id, tmp_path)
    round1_dir = iterations_root / round1_run_id
    with StateStore(round1_dir / "state.sqlite", read_only=True) as state:
        attempts_before = state.max_attempt_per_node(round1_run_id)

    _interrupt_after_round(first.group_id, tmp_path, new_max=5)
    resumed = await resume_adversarial_verification(first.group_id, base=tmp_path)

    assert resumed.status == "accepted", "the resumed loop ran round 2 and accepted"
    assert len(resumed.iterations) == 2
    assert resumed.iterations[0].run_id == round1_run_id, "round 1 keeps its run id"

    with StateStore(round1_dir / "state.sqlite", read_only=True) as state:
        attempts_after = state.max_attempt_per_node(round1_run_id)
    assert attempts_after == attempts_before, "round 1's completed Run was not re-attempted"


@pytest.mark.asyncio
async def test_resuming_a_finished_accepted_group_is_refused(tmp_path: Path) -> None:
    _write_fixture(tmp_path / "accept.fixture.json", accept=True)
    workflow = _write_workflow(tmp_path, "accept.fixture.json")

    result = await run_adversarial_verification(_spec(workflow, max_rounds=3), base=tmp_path)
    assert result.status == "accepted"

    with pytest.raises(ControllerError, match="already finished"):
        await resume_adversarial_verification(result.group_id, base=tmp_path)


@pytest.mark.asyncio
async def test_resuming_a_rejected_group_is_refused(tmp_path: Path) -> None:
    _write_fixture(tmp_path / "loop.fixture.json", accept=False, next_fixture="loop.fixture.json")
    workflow = _write_workflow(tmp_path, "loop.fixture.json")

    result = await run_adversarial_verification(_spec(workflow, max_rounds=2), base=tmp_path)
    assert result.status == "rejected", "the loop hit the cap without acceptance"

    with pytest.raises(ControllerError, match="already finished"):
        await resume_adversarial_verification(result.group_id, base=tmp_path)


@pytest.mark.asyncio
async def test_resuming_a_failed_group_reruns_the_failed_round_and_continues(
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "round.fixture.json"
    fixture.write_text(json.dumps({"exit_status": 1, "stderr": "boom"}), encoding="utf-8")
    workflow = _write_workflow(tmp_path, "round.fixture.json")

    first = await run_adversarial_verification(_spec(workflow, max_rounds=3), base=tmp_path)
    assert first.status == "failed"
    failed_run_id = first.iterations[0].run_id

    _write_fixture(fixture, accept=True)
    resumed = await resume_adversarial_verification(first.group_id, base=tmp_path)

    assert resumed.status == "accepted", "the re-run round now succeeds and accepts"
    assert resumed.iterations[0].run_id == failed_run_id, "the failed round resumes in place"

    persisted = load_group_state(first.group_id, tmp_path)
    assert persisted["status"] == "accepted"
