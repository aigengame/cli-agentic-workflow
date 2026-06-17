"""Adversarial-verification Pattern Controller seam (#17, ADR 0002 / 0009).

Adversarial verification runs a generator, runs verifier nodes against the result,
and either ACCEPTS (stops, status ``accepted``), or REJECTS and REGENERATES via a
new Run in the same Run Group with the verifier's feedback substituted into the
next generation (structural substitution, not templating). It is realized as a
Pattern Controller on the SAME Run Group infrastructure ``loop_until_done`` uses
(immutable per-iteration Runs, the membership mirror, the group-state file), with
its own accept/reject verdict vocabulary and stop reasons.

These tests run the whole verification OFFLINE with the mock Adapter (fixtures), so
a deterministic multi-round accept/reject loop is proven with no tokens: a faithful
feedback loop where round N's verifier verdict selects round N+1's generator
fixture, the accept Predicate reads the verifier's verdict, and the loop stops on
accept / failure / max-rounds.
"""

import json
from pathlib import Path

import pytest

from caw.controller import (
    AdversarialSpec,
    GroupResult,
    run_adversarial_verification,
)
from caw.runlayout import group_iterations_root, group_state_path
from caw.state import StateStore

# ----------------------------------------------------------------------------------
# A verification round is a single mock-Adapter agent Node named ``verify``. Its
# fixture is a canned normalized result whose ``stdout`` carries the verdict
# (the accept Predicate reads it with ``contains`` — ACCEPT vs REJECT) and whose
# ``structured_output`` carries ``next_fixture`` (the generator fixture the next
# round should read after a REJECT). The Controller's feedback substitutes
# ``verify.structured_output.next_fixture`` into the ``verify`` node's ``fixture``
# field for the next round — a faithful, deterministic regeneration loop offline.
# ----------------------------------------------------------------------------------


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
        "      prompt: Verify the generated result; accept it or reject it.\n"
        f"      fixture: {first_fixture}\n",
        encoding="utf-8",
    )
    return workflow


def _spec(
    workflow: Path, *, max_rounds: int, reject: dict[str, object] | None = None
) -> AdversarialSpec:
    # The accept Predicate reuses the existing `when` algebra: a result is ACCEPTED
    # when the `verify` node's textual `stdout` contains the ACCEPT verdict
    # (`contains` is the op valid on the string `stdout` field, #7). A non-accepted
    # round REJECTS and regenerates with feedback fed forward into the next round —
    # unless the OPTIONAL `reject` Predicate holds, which terminates the group as an
    # explicit verifier reject (distinct from cap-exhaustion).
    raw: dict[str, object] = {
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
    if reject is not None:
        raw["reject"] = reject
    return AdversarialSpec.model_validate(raw)


def test_spec_rejects_an_accept_ref_that_misses_verify_node() -> None:
    # The accept Predicate may only reference `verify_node`: at evaluation time only
    # that node's output is supplied to the Predicate, so a ref to a typo or a
    # different node would silently evaluate false and the loop would wrongly REJECT
    # forever. `AdversarialSpec.model_validate` must reject it (fail fast over fail
    # silent), mirroring `ControllerSpec`'s done-ref invariant.
    with pytest.raises(ValueError, match="accept predicate references node 'verfy'"):
        AdversarialSpec.model_validate(
            {
                "workflow": "iteration.yaml",
                "max_rounds": 5,
                "verify_node": "verify",
                "accept": {
                    "ref": {"node": "verfy", "field": "stdout"},
                    "op": "contains",
                    "value": "ACCEPT",
                },
            }
        )


@pytest.mark.asyncio
async def test_verification_stops_when_a_round_accepts(tmp_path: Path) -> None:
    # AC1: round 1 rejects and points to round2.fixture.json; the Controller feeds
    # that forward; round 2 reads it and accepts, so the loop stops `accepted` —
    # each round a separate immutable Run in the same Run Group.
    _write_fixture(
        tmp_path / "round1.fixture.json", accept=False, next_fixture="round2.fixture.json"
    )
    _write_fixture(tmp_path / "round2.fixture.json", accept=True)
    workflow = _write_workflow(tmp_path, "round1.fixture.json")
    spec = _spec(workflow, max_rounds=5)

    result = await run_adversarial_verification(spec, base=tmp_path)

    assert isinstance(result, GroupResult)
    assert result.status == "accepted", "the loop stopped because the accept Predicate held"
    assert len(result.iterations) == 2, "exactly two rounds materialized"
    iterations_root = group_iterations_root(result.group_id, tmp_path)
    iteration_run_dirs = sorted(p.name for p in iterations_root.iterdir())
    assert iteration_run_dirs == sorted(it.run_id for it in result.iterations)


@pytest.mark.asyncio
async def test_verification_rejects_until_max_rounds(tmp_path: Path) -> None:
    # AC1: a result that is never accepted REGENERATES every round and stops at
    # max_rounds with the terminal `rejected` status, rather than looping forever.
    _write_fixture(tmp_path / "loop.fixture.json", accept=False, next_fixture="loop.fixture.json")
    workflow = _write_workflow(tmp_path, "loop.fixture.json")
    spec = _spec(workflow, max_rounds=3)

    result = await run_adversarial_verification(spec, base=tmp_path)

    assert result.status == "rejected", "the loop stopped at the round cap without acceptance"
    assert len(result.iterations) == 3, "exactly max_rounds runs materialized"


@pytest.mark.asyncio
async def test_explicit_reject_predicate_terminates_the_group_as_rejected(tmp_path: Path) -> None:
    # AC1: the THREE outcomes are accept / reject / regenerate. An explicit `reject`
    # Predicate that holds over a round terminates the group `rejected` IMMEDIATELY —
    # a verifier reject, distinct from cap-exhaustion — rather than regenerating until
    # the cap. round 1's verdict is REJECT, so the reject Predicate holds on round 1
    # and the group stops after exactly one round (no regeneration to the cap).
    _write_fixture(
        tmp_path / "round1.fixture.json", accept=False, next_fixture="round2.fixture.json"
    )
    _write_fixture(tmp_path / "round2.fixture.json", accept=True)
    workflow = _write_workflow(tmp_path, "round1.fixture.json")
    spec = _spec(
        workflow,
        max_rounds=5,
        reject={"ref": {"node": "verify", "field": "stdout"}, "op": "contains", "value": "REJECT"},
    )

    result = await run_adversarial_verification(spec, base=tmp_path)

    assert result.status == "rejected", "the explicit reject Predicate stopped the loop"
    assert len(result.iterations) == 1, "the reject terminates at round 1, not the cap"
    persisted = json.loads(group_state_path(result.group_id, tmp_path).read_text())
    assert persisted["status"] == "rejected"


@pytest.mark.asyncio
async def test_accept_wins_over_reject_when_both_could_hold(tmp_path: Path) -> None:
    # Per-round order is accept -> reject -> regenerate: when a round could satisfy
    # BOTH accept and reject, accept wins. round 1's verdict is ACCEPT, and the reject
    # Predicate would hold on any non-empty stdout, but accept is checked first.
    _write_fixture(tmp_path / "round1.fixture.json", accept=True)
    workflow = _write_workflow(tmp_path, "round1.fixture.json")
    spec = _spec(
        workflow,
        max_rounds=5,
        # A reject Predicate broad enough to also hold on an ACCEPT verdict.
        reject={"ref": {"node": "verify", "field": "exit_status"}, "op": "equals", "value": 0},
    )

    result = await run_adversarial_verification(spec, base=tmp_path)

    assert result.status == "accepted", "accept is evaluated before reject"
    assert len(result.iterations) == 1


def test_spec_rejects_a_reject_ref_that_misses_verify_node() -> None:
    # The OPTIONAL `reject` Predicate is symmetric with `accept`: its leaf refs may
    # only address `verify_node`, validated identically (fail fast over fail silent).
    with pytest.raises(ValueError, match="reject predicate references node 'verfy'"):
        AdversarialSpec.model_validate(
            {
                "workflow": "iteration.yaml",
                "max_rounds": 5,
                "verify_node": "verify",
                "accept": {
                    "ref": {"node": "verify", "field": "stdout"},
                    "op": "contains",
                    "value": "ACCEPT",
                },
                "reject": {
                    "ref": {"node": "verfy", "field": "stdout"},
                    "op": "contains",
                    "value": "REJECT",
                },
            }
        )


@pytest.mark.asyncio
async def test_verification_stops_on_a_failed_round(tmp_path: Path) -> None:
    # AC1: a failed round stops the loop (no point feeding a failed result forward).
    (tmp_path / "fail.fixture.json").write_text(
        json.dumps({"exit_status": 1, "stderr": "boom"}), encoding="utf-8"
    )
    workflow = _write_workflow(tmp_path, "fail.fixture.json")
    spec = _spec(workflow, max_rounds=5)

    result = await run_adversarial_verification(spec, base=tmp_path)

    assert result.status == "failed", "the loop stopped on the failed round"
    assert len(result.iterations) == 1, "no round is materialized after the failure"


@pytest.mark.asyncio
async def test_each_round_records_its_group_membership(tmp_path: Path) -> None:
    # AC3-parity: each round's Run records the run group id and round index in its
    # own State; the controller state is persisted in group.json.
    _write_fixture(
        tmp_path / "round1.fixture.json", accept=False, next_fixture="round2.fixture.json"
    )
    _write_fixture(tmp_path / "round2.fixture.json", accept=True)
    workflow = _write_workflow(tmp_path, "round1.fixture.json")
    spec = _spec(workflow, max_rounds=5)

    result = await run_adversarial_verification(spec, base=tmp_path)

    iterations_root = group_iterations_root(result.group_id, tmp_path)
    for index, iteration in enumerate(result.iterations):
        run_dir = iterations_root / iteration.run_id
        with StateStore(run_dir / "state.sqlite") as state:
            membership = state.run_group_membership(iteration.run_id)
        assert membership == (result.group_id, index)

    persisted = json.loads(group_state_path(result.group_id, tmp_path).read_text())
    assert persisted["status"] == "accepted"
    assert [it["run_id"] for it in persisted["iterations"]] == [
        it.run_id for it in result.iterations
    ]


@pytest.mark.asyncio
async def test_verifier_feedback_is_substituted_into_the_next_round(tmp_path: Path) -> None:
    # AC1: the verifier's feedback from round N reaches round N+1's generator. The
    # second round's FROZEN snapshot must show the substituted fixture path —
    # proving the rejection's feedback reached the materialized regeneration Run.
    _write_fixture(
        tmp_path / "round1.fixture.json", accept=False, next_fixture="round2.fixture.json"
    )
    _write_fixture(tmp_path / "round2.fixture.json", accept=True)
    workflow = _write_workflow(tmp_path, "round1.fixture.json")
    spec = _spec(workflow, max_rounds=5)

    result = await run_adversarial_verification(spec, base=tmp_path)

    iterations_root = group_iterations_root(result.group_id, tmp_path)
    second_run_dir = iterations_root / result.iterations[1].run_id
    snapshot = json.loads((second_run_dir / "workflow.normalized.json").read_text())
    verify = next(n for n in snapshot["workflow"]["nodes"] if n["id"] == "verify")
    assert verify["inputs"]["fixture"].endswith("round2.fixture.json"), (
        "round 2's frozen workflow carries the verifier feedback fed forward from round 1"
    )
