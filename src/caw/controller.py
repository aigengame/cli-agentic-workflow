"""Pattern Controllers: sequence immutable Runs into a Run Group (ADR 0002 / 0009).

A **Pattern Controller** expresses iterative behavior by evaluating a finished Run
and materializing the next one; the kernel itself only executes acyclic Runs
(CONTEXT.md, ADR 0002). This module realizes the first Controller, ``loop_until_done``:
it drives the EXISTING ``execute_run`` / ``resume_run`` as black boxes, materializing
each iteration as a separate immutable Run under a Run Group, and stops on done /
failure / max-iterations.

The Controller is NOT in the IR and is NOT an Expander (ADR 0008): it lives above the
executor, in Python. The loop is described by a :class:`ControllerSpec` (a separate
authored surface from the iteration ``Workflow``), the done Predicate reuses the
existing ``when`` Predicate algebra (the sole conditional mechanism, ADR 0007), and
feedback from iteration N reaches iteration N+1 by STRUCTURAL substitution of the
prior Run's terminal ``structured_output`` into a named node input — not string
templating (ADR 0009). Controller state is persisted authoritatively in the Run
Group's ``group.json``; each iteration's Run also records its membership in State
(AC3), a queryable mirror.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from caw.adapter import AdapterRegistry
from caw.config import WorkflowConfigError, load_workflow_file
from caw.executor import RunResult, execute_run, is_resumable, resume_run
from caw.model import Predicate, normalize_workflow
from caw.predicate import evaluate_predicate
from caw.runlayout import (
    group_dir,
    group_iterations_root,
    group_state_path,
)
from caw.state import StateStore

# The Run Group's status (ADR 0009).
#   TERMINAL (resume refused — nothing to do):
#     "done"      — the done Predicate held over the finished iteration's output
#     "exhausted" — the iteration index reached max_iterations without done
#   RESUMABLE (resume continues the loop):
#     "running"   — an in-progress marker persisted BETWEEN iterations; an
#                   interrupted group is found in this state and the loop continues
#     "failed"    — an iteration's Run failed (resumable per Resume Eligibility:
#                   resume re-runs it in place, then the loop continues)
GROUP_DONE = "done"
GROUP_EXHAUSTED = "exhausted"
GROUP_FAILED = "failed"
GROUP_RUNNING = "running"


class FeedbackSpec(BaseModel):
    """How a Run Group passes feedback from iteration N to iteration N+1 (AC2).

    Structural substitution, not string templating (ADR 0009): after iteration N,
    the Controller reads ``from_field`` off the evaluate-node's ``structured_output``
    and substitutes its value into ``to_field`` of node ``to_node`` BEFORE
    materializing iteration N+1. Iteration 1 carries no feedback (no prior Run).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    to_node: str
    to_field: str
    # The field of the evaluate-node's structured_output whose value is fed forward.
    from_field: str


class ControllerSpec(BaseModel):
    """The authored definition of a loop-until-done Run Group (ADR 0009).

    ``workflow`` is the iteration ``Workflow`` file (an ordinary single-iteration
    graph); ``max_iterations`` is the hard cap; ``done`` is the structured done
    Predicate (the ``when`` algebra) evaluated against ``evaluate_node``'s output;
    ``feedback`` (optional) carries the prior iteration's output into the next.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    workflow: Path
    max_iterations: int = Field(ge=1)
    # The id of the Run's node whose normalized output the done Predicate and the
    # feedback source read. A Run can have several leaf nodes, so the spec names the
    # one carrying the iteration's verdict explicitly, mirroring a `when` ref's node.
    evaluate_node: str
    done: Predicate
    feedback: FeedbackSpec | None = None

    @model_validator(mode="after")
    def _done_refs_must_target_evaluate_node(self) -> "ControllerSpec":
        # Every leaf ``ref`` in the done Predicate must address ``evaluate_node``:
        # at evaluation time only that node's output is supplied to the Predicate
        # (``_iteration_verdict`` returns ``None`` for any other node), so a ref to a
        # typo or a different node would silently evaluate false and the loop would
        # wrongly EXHAUST. Reject it at validation, mirroring ``model.py``'s
        # ``when``-refs-must-be-in-``needs`` invariant — fail fast over fail silent.
        for ref in self.done.leaf_refs():
            if ref.node != self.evaluate_node:
                raise ValueError(
                    f"done predicate references node {ref.node!r}, but only "
                    f"evaluate_node {self.evaluate_node!r}'s output is available to it"
                )
        return self


@dataclass(frozen=True)
class IterationResult:
    """One iteration of a Run Group: its run id and whether its Run succeeded."""

    iteration_index: int
    run_id: str
    succeeded: bool


@dataclass(frozen=True)
class GroupResult:
    """The outcome of one Pattern Controller's Run Group execution (#15, #17).

    ``status`` is why the controller stopped — its meaning is per-controller:
    ``loop_until_done`` uses ``done`` / ``exhausted`` / ``failed``; adversarial
    verification uses ``accepted`` / ``rejected`` / ``failed``; the tournament uses
    ``complete`` / ``failed``. ``iterations`` lists every materialized iteration
    (loop iteration / verification round / tournament round) in order. ``winner``
    is the tournament's final promoted candidate (``None`` for the other
    controllers, which name no winner).
    """

    group_id: str
    status: str
    iterations: tuple[IterationResult, ...]
    winner: str | None = None


class ControllerError(Exception):
    """Raised when a Run Group cannot be materialized or resumed (#15).

    A controller-class refusal: an unreadable/invalid spec, an unknown or
    already-finished group, or an evaluate-node the iteration workflow lacks.
    """


def load_controller_spec(spec_file: Path) -> ControllerSpec:
    """Load and validate a controller spec file, folding failures into a one-liner.

    ``workflow`` is anchored to the SPEC file's directory so the same spec resolves
    its iteration workflow identically from any cwd (mirroring how agent-Node paths
    anchor to the workflow file's directory, #64).
    """
    raw = load_workflow_file(spec_file)
    workflow_value = raw.get("workflow")
    if isinstance(workflow_value, str) and not Path(workflow_value).is_absolute():
        raw = {**raw, "workflow": str(spec_file.resolve().parent / workflow_value)}
    try:
        return ControllerSpec.model_validate(raw)
    except Exception as exc:  # noqa: BLE001 — fold any validation failure to one line
        raise WorkflowConfigError(
            f"invalid controller spec in {spec_file}: {_first_line(exc)}"
        ) from exc


def _first_line(exc: Exception) -> str:
    """The first line of an exception's message, keeping the CLI's one-line contract."""
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _new_group_id() -> str:
    import secrets
    from datetime import UTC, datetime

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"group-{timestamp}-{secrets.token_hex(4)}"


# One structural substitution: replace ``to_field`` of node ``to_node``'s inputs
# with ``value`` BEFORE the iteration is normalized and frozen (ADR 0009). The
# Controllers compose a list of these — loop/adversarial feed one (feedback), the
# tournament feeds two (the promoted winner + the next fixture) — so the single
# substitution primitive serves every Controller rather than each re-implementing it.
@dataclass(frozen=True)
class _Substitution:
    to_node: str
    to_field: str
    value: object


def _feedback_substitution(
    feedback: "FeedbackSpec | None", value: object | None
) -> tuple[_Substitution, ...]:
    """The (at most one) substitution a feedback spec contributes for an iteration.

    Iteration 1 (``value is None``) or a spec with no ``feedback`` contributes none, so
    the base workflow is reused unchanged (a pure idempotent loop). Otherwise the
    fed-forward value is substituted into ``feedback.to_field`` of ``feedback.to_node``.
    """
    if feedback is None or value is None:
        return ()
    return (_Substitution(to_node=feedback.to_node, to_field=feedback.to_field, value=value),)


def _materialize_iteration_raw(
    base_raw: dict[str, Any], substitutions: tuple[_Substitution, ...]
) -> dict[str, Any]:
    """Build iteration N's raw workflow mapping, applying structural substitutions.

    With no substitutions the base workflow is returned unchanged. Otherwise each
    substitution replaces a NAMED node's NAMED input field with a fed-forward value
    BEFORE the run is normalized and frozen, so the materialized run carries the prior
    iteration's feedback (ADR 0009, structural substitution — never string templating).
    Feedback requires a ``nodes:`` workflow (a ``pattern:`` workflow cannot carry it in
    v0.1), and a substitution target absent from the workflow is a controller refusal.
    """
    if not substitutions:
        return base_raw
    nodes = base_raw.get("nodes")
    if not isinstance(nodes, list):
        raise ControllerError(
            "feedback requires a `nodes:` iteration workflow; "
            "a `pattern:` workflow cannot carry feedback in v0.1"
        )
    by_node: dict[str, list[_Substitution]] = {}
    for substitution in substitutions:
        by_node.setdefault(substitution.to_node, []).append(substitution)
    substituted_nodes: list[Any] = []
    found: set[str] = set()
    for node in nodes:
        node_id = node.get("id") if isinstance(node, dict) else None
        applicable = by_node.get(node_id) if node_id is not None else None
        if isinstance(node, dict) and applicable:
            found.add(node_id)
            inputs = dict(node.get("inputs", {}))
            for substitution in applicable:
                inputs[substitution.to_field] = substitution.value
            substituted_nodes.append({**node, "inputs": inputs})
        else:
            substituted_nodes.append(node)
    missing = sorted(set(by_node) - found)
    if missing:
        raise ControllerError(
            f"feedback target node {missing[0]!r} is not in the iteration workflow"
        )
    return {**base_raw, "nodes": substituted_nodes}


def _evaluate_node_output(run_dir: Path, run_id: str, node_id: str) -> dict[str, Any] | None:
    """Read the evaluate-node's persisted normalized output from a finished Run's State."""
    with StateStore(run_dir / "state.sqlite", read_only=True) as state:
        return state.node_output(run_id, node_id)


def _feedback_value(output: dict[str, Any] | None, from_field: str) -> object | None:
    """Extract the fed-forward value from the evaluate-node's structured_output.

    Returns ``None`` when the node produced no structured output or lacks the field,
    so a loop with nothing to feed forward simply reuses the base workflow next time.
    """
    if output is None:
        return None
    structured = output.get("structured_output")
    if not isinstance(structured, dict):
        return None
    return structured.get(from_field)


async def run_loop_until_done(
    spec: ControllerSpec, base: Path, registry: AdapterRegistry | None = None
) -> GroupResult:
    """Drive a loop-until-done Run Group to completion (ADR 0002 / 0009).

    Materializes each iteration as a separate immutable Run under
    ``<base>/.caw/groups/<group_id>/iterations/``, feeding the prior iteration's
    output forward, and stops on done / failure / max-iterations. Controller state
    is persisted to ``group.json`` after each iteration; each iteration's Run also
    records its group membership in State (AC3).
    """
    group_id = _new_group_id()
    group_dir(group_id, base).mkdir(parents=True, exist_ok=True)
    return await _drive_loop(spec, base, group_id, registry, iterations=(), feedback_value=None)


async def _drive_loop(
    spec: ControllerSpec,
    base: Path,
    group_id: str,
    registry: AdapterRegistry | None,
    *,
    iterations: tuple[IterationResult, ...],
    feedback_value: object | None,
) -> GroupResult:
    """The iteration loop, shared by a fresh run and a group resume.

    ``iterations`` seeds the already-completed iterations (empty for a fresh group);
    ``feedback_value`` seeds the value to feed into the next iteration. The loop
    materializes iterations until the done Predicate holds, an iteration fails, or
    the iteration index reaches ``max_iterations``.
    """
    resolved_registry = registry or AdapterRegistry()
    iterations_root = group_iterations_root(group_id, base)
    iterations_root.mkdir(parents=True, exist_ok=True)
    base_raw = load_workflow_file(spec.workflow)
    base_dir = spec.workflow.resolve().parent
    completed = list(iterations)

    while len(completed) < spec.max_iterations:
        index = len(completed)
        iteration_raw = _materialize_iteration_raw(
            base_raw, _feedback_substitution(spec.feedback, feedback_value)
        )
        workflow = normalize_workflow(
            iteration_raw, source=f"{spec.workflow} (iteration {index})", base_dir=base_dir
        )
        if not any(node.id == spec.evaluate_node for node in workflow.nodes):
            raise ControllerError(
                f"evaluate_node {spec.evaluate_node!r} is not in the iteration workflow"
            )
        result = await execute_run(workflow, iterations_root, resolved_registry)
        run_dir = iterations_root / result.run_id
        _record_membership(run_dir, result.run_id, group_id, index)
        completed.append(
            IterationResult(
                iteration_index=index, run_id=result.run_id, succeeded=result.succeeded
            )
        )

        status = _iteration_verdict(spec, run_dir, result, index)
        if status is not None:
            _persist_group_state(spec, base, group_id, tuple(completed), status)
            return GroupResult(group_id=group_id, status=status, iterations=tuple(completed))

        # Not yet done and not the last iteration: feed this iteration's output
        # forward and persist the in-progress (RUNNING) group state before the next
        # run, so an interruption here is distinguishable from a terminal EXHAUSTED.
        feedback_value = _next_feedback(spec, run_dir, result.run_id)
        _persist_group_state(spec, base, group_id, tuple(completed), GROUP_RUNNING)

    # The cap was reached without done: this EXHAUSTED is terminal.
    _persist_group_state(spec, base, group_id, tuple(completed), GROUP_EXHAUSTED)
    return GroupResult(group_id=group_id, status=GROUP_EXHAUSTED, iterations=tuple(completed))


def _iteration_verdict(
    spec: ControllerSpec, run_dir: Path, result: RunResult, index: int
) -> str | None:
    """The terminal group status if the loop should stop after this iteration, else None.

    Stops on a failed Run, or on the done Predicate holding over the evaluate-node's
    output. Returns ``None`` to continue (the caller also stops at max_iterations).
    """
    if not result.succeeded:
        return GROUP_FAILED
    output = _evaluate_node_output(run_dir, result.run_id, spec.evaluate_node)

    def output_of(node_id: str) -> dict[str, Any] | None:
        return output if node_id == spec.evaluate_node else None

    if evaluate_predicate(spec.done, output_of):
        return GROUP_DONE
    return None


def _next_feedback(spec: ControllerSpec, run_dir: Path, run_id: str) -> object | None:
    """The value to feed into the next iteration: the evaluate-node's fed-forward field.

    ``None`` when the spec declares no feedback or the field is absent, so the next
    iteration reuses the base workflow unchanged.
    """
    if spec.feedback is None:
        return None
    output = _evaluate_node_output(run_dir, run_id, spec.evaluate_node)
    return _feedback_value(output, spec.feedback.from_field)


def _record_membership(run_dir: Path, run_id: str, group_id: str, index: int) -> None:
    """Write the iteration Run's group membership row by re-opening its State (AC3).

    ``execute_run`` closes the iteration's State on return, so the Controller
    re-opens it to record the denormalized membership mirror; ``group.json`` stays
    authoritative for control flow (ADR 0009).
    """
    with StateStore(run_dir / "state.sqlite") as state:
        state.record_run_group_membership(
            run_id=run_id, run_group_id=group_id, iteration_index=index
        )


def _persist_group_state(
    spec: ControllerSpec,
    base: Path,
    group_id: str,
    iterations: tuple[IterationResult, ...],
    status: str,
) -> None:
    """Persist a ``loop_until_done`` Run Group's authoritative controller state.

    Records the group id, the spec, the ordered iterations (run id + outcome), the
    iteration index, and the group status — the resumable source of truth (ADR 0002).
    """
    _write_group_state(base, group_id, spec.model_dump(mode="json"), iterations, status)


def _write_group_state(
    base: Path,
    group_id: str,
    spec_dump: dict[str, Any],
    iterations: tuple[IterationResult, ...],
    status: str,
    *,
    extra_iteration_fields: dict[str, dict[str, Any]] | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Write a Run Group's authoritative ``group.json``, shared by every Controller.

    ``spec_dump`` is the serialized controller spec (so a resume reconstructs the
    loop from it); ``iterations`` is the ordered per-iteration outcomes. A Controller
    may attach extra per-iteration fields (``extra_iteration_fields`` keyed by run id —
    e.g. the tournament's promoted winner) and extra top-level fields (``extra`` — e.g.
    the tournament's final winner), keeping ``group.json`` the single authoritative,
    queryable source of the group's control flow and result.
    """
    per_iteration = extra_iteration_fields or {}
    state: dict[str, Any] = {
        "group_id": group_id,
        "spec": spec_dump,
        "iteration_index": len(iterations),
        "status": status,
        "iterations": [
            {
                "iteration_index": it.iteration_index,
                "run_id": it.run_id,
                "succeeded": it.succeeded,
                **per_iteration.get(it.run_id, {}),
            }
            for it in iterations
        ],
        **(extra or {}),
    }
    group_state_path(group_id, base).write_text(
        json.dumps(state, indent=2) + "\n", encoding="utf-8"
    )


def load_group_state(group_id: str, base: Path) -> dict[str, Any]:
    """Read a Run Group's persisted controller state, or refuse if absent (#15)."""
    state_path = group_state_path(group_id, base)
    if not state_path.is_file():
        raise ControllerError(
            f"no run group for group id {group_id!r} under {group_dir(group_id, base).parent}"
        )
    loaded: dict[str, Any] = json.loads(state_path.read_text(encoding="utf-8"))
    return loaded


async def resume_loop_until_done(
    group_id: str, base: Path, registry: AdapterRegistry | None = None
) -> GroupResult:
    """Resume an interrupted Run Group at the GROUP level (AC5, ADR 0002 / 0009).

    Re-reads ``group.json`` and resumes only a non-terminal Run Group. A TERMINAL
    group is refused: ``done`` (the done Predicate held) and ``exhausted`` (the cap
    was reached) both have nothing left to do. A RESUMABLE group continues: a
    ``running`` group was interrupted between iterations, so the loop continues from
    the persisted iteration index; a ``failed`` group's last iteration Run is
    resumable (Resume Eligibility, CONTEXT.md), so ``resume_run`` re-runs it in place
    and the loop continues. A SUCCEEDED iteration Run is never re-run; if the last
    recorded iteration's Run is incomplete it is ``resume_run``'d in place, then the
    loop continues with feedback fed forward from the last completed iteration.
    """
    persisted = load_group_state(group_id, base)
    spec = ControllerSpec.model_validate(persisted["spec"])
    resolved_registry = registry or AdapterRegistry()
    iterations_root = group_iterations_root(group_id, base)

    status = persisted["status"]
    if status in {GROUP_DONE, GROUP_EXHAUSTED}:
        raise ControllerError(
            f"run group {group_id!r} already finished (status: {status}); nothing to resume"
        )

    completed = [
        IterationResult(
            iteration_index=it["iteration_index"],
            run_id=it["run_id"],
            succeeded=it["succeeded"],
        )
        for it in persisted["iterations"]
    ]

    # The last recorded iteration may be incomplete (the group was interrupted
    # mid-iteration). A succeeded iteration is left untouched; an incomplete one is
    # resumed in place, reusing its run id, State, and Events trace.
    if completed:
        last = completed[-1]
        last_run_dir = iterations_root / last.run_id
        with StateStore(last_run_dir / "state.sqlite", read_only=True) as state:
            last_status = state.run_status(last.run_id)
        if is_resumable(last_status):
            resumed = await resume_run(last.run_id, iterations_root, resolved_registry)
            completed[-1] = IterationResult(
                iteration_index=last.iteration_index,
                run_id=resumed.run_id,
                succeeded=resumed.succeeded,
            )
            _record_membership(last_run_dir, resumed.run_id, group_id, last.iteration_index)
            verdict = _iteration_verdict(spec, last_run_dir, resumed, last.iteration_index)
            if verdict is not None:
                _persist_group_state(spec, base, group_id, tuple(completed), verdict)
                return GroupResult(group_id=group_id, status=verdict, iterations=tuple(completed))

    # Feed the last completed iteration's output forward and continue the loop.
    feedback_value: object | None = None
    if completed:
        last = completed[-1]
        feedback_value = _next_feedback(spec, iterations_root / last.run_id, last.run_id)

    return await _drive_loop(
        spec,
        base,
        group_id,
        resolved_registry,
        iterations=tuple(completed),
        feedback_value=feedback_value,
    )


# ======================================================================================
# Adversarial verification (#17)
# ======================================================================================
#
# Adversarial verification runs a generator, runs verifier nodes against the result,
# and either ACCEPTS the result (stops) or REJECTS it and REGENERATES via a new Run in
# the same Run Group with the verifier's feedback fed forward. It is a Pattern
# Controller on the SAME Run Group infrastructure ``loop_until_done`` uses — immutable
# per-round Runs, the membership mirror, the ``group.json`` control state, and
# structural feedback substitution (ADR 0009, NO string templating) — with its own
# accept/reject verdict vocabulary and stop reasons.

# The adversarial Run Group's status (#17), at parity with the loop-until-done set:
#   TERMINAL (resume refused — nothing to do):
#     "accepted" — the accept Predicate held over a round's verifier output
#     "rejected" — the round index reached max_rounds without an acceptance
#   RESUMABLE (resume continues the verification):
#     "running"  — an in-progress marker persisted BETWEEN rounds
#     "failed"   — a round's Run failed (resumable per Resume Eligibility)
GROUP_ACCEPTED = "accepted"
GROUP_REJECTED = "rejected"


class AdversarialSpec(BaseModel):
    """The authored definition of an adversarial-verification Run Group (#17).

    ``workflow`` is the round ``Workflow`` file (an ordinary single-iteration graph
    carrying the generator and the verifier nodes); ``max_rounds`` is the hard cap on
    regeneration rounds; ``verify_node`` is the id of the node whose normalized output
    the ``accept`` Predicate (the ``when`` algebra, ADR 0007) reads; ``accept`` holds
    when the result is accepted, stopping the loop; ``feedback`` (optional) carries the
    verifier's critique into the next round's generation (structural substitution).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    workflow: Path
    max_rounds: int = Field(ge=1)
    # The id of the node whose normalized output the accept Predicate and the feedback
    # source read — the round's verdict carrier, mirroring a `when` ref's node and
    # ControllerSpec's `evaluate_node`.
    verify_node: str
    accept: Predicate
    feedback: FeedbackSpec | None = None

    @model_validator(mode="after")
    def _accept_refs_must_target_verify_node(self) -> "AdversarialSpec":
        # Every leaf ``ref`` in the accept Predicate must address ``verify_node``: at
        # evaluation time only that node's output is supplied to the Predicate, so a ref
        # to a typo or a different node would silently evaluate false and the loop would
        # wrongly REJECT forever. Reject it at validation (fail fast over fail silent),
        # mirroring ControllerSpec's done-ref invariant.
        for ref in self.accept.leaf_refs():
            if ref.node != self.verify_node:
                raise ValueError(
                    f"accept predicate references node {ref.node!r}, but only "
                    f"verify_node {self.verify_node!r}'s output is available to it"
                )
        return self


async def run_adversarial_verification(
    spec: AdversarialSpec, base: Path, registry: AdapterRegistry | None = None
) -> GroupResult:
    """Drive an adversarial-verification Run Group to completion (#17, ADR 0002 / 0009).

    Materializes each verification round as a separate immutable Run under the group's
    ``iterations/`` root, feeding the verifier's feedback forward into the next round's
    generation, and stops when the accept Predicate holds (``accepted``), a round's Run
    fails (``failed``), or the round index reaches ``max_rounds`` (``rejected``).
    """
    group_id = _new_group_id()
    group_dir(group_id, base).mkdir(parents=True, exist_ok=True)
    return await _drive_adversarial(
        spec, base, group_id, registry, iterations=(), feedback_value=None
    )


async def _drive_adversarial(
    spec: AdversarialSpec,
    base: Path,
    group_id: str,
    registry: AdapterRegistry | None,
    *,
    iterations: tuple[IterationResult, ...],
    feedback_value: object | None,
) -> GroupResult:
    """The verification round loop, shared by a fresh run and a group resume."""
    resolved_registry = registry or AdapterRegistry()
    iterations_root = group_iterations_root(group_id, base)
    iterations_root.mkdir(parents=True, exist_ok=True)
    base_raw = load_workflow_file(spec.workflow)
    base_dir = spec.workflow.resolve().parent
    completed = list(iterations)

    while len(completed) < spec.max_rounds:
        index = len(completed)
        round_raw = _materialize_iteration_raw(
            base_raw, _feedback_substitution(spec.feedback, feedback_value)
        )
        workflow = normalize_workflow(
            round_raw, source=f"{spec.workflow} (round {index})", base_dir=base_dir
        )
        if not any(node.id == spec.verify_node for node in workflow.nodes):
            raise ControllerError(
                f"verify_node {spec.verify_node!r} is not in the round workflow"
            )
        result = await execute_run(workflow, iterations_root, resolved_registry)
        run_dir = iterations_root / result.run_id
        _record_membership(run_dir, result.run_id, group_id, index)
        completed.append(
            IterationResult(
                iteration_index=index, run_id=result.run_id, succeeded=result.succeeded
            )
        )

        status = _verification_verdict(spec, run_dir, result)
        if status is not None:
            _persist_adversarial_state(spec, base, group_id, tuple(completed), status)
            return GroupResult(group_id=group_id, status=status, iterations=tuple(completed))

        # Rejected and not the last round: feed the verifier's feedback forward and
        # persist the in-progress (RUNNING) state before the next round, so an
        # interruption here reads distinctly from the terminal REJECTED.
        feedback_value = _adversarial_feedback(spec, run_dir, result.run_id)
        _persist_adversarial_state(spec, base, group_id, tuple(completed), GROUP_RUNNING)

    # The cap was reached without an acceptance: this REJECTED is terminal.
    _persist_adversarial_state(spec, base, group_id, tuple(completed), GROUP_REJECTED)
    return GroupResult(group_id=group_id, status=GROUP_REJECTED, iterations=tuple(completed))


def _verification_verdict(
    spec: AdversarialSpec, run_dir: Path, result: RunResult
) -> str | None:
    """The terminal status if the verification should stop after this round, else None.

    Stops on a failed Run, or on the accept Predicate holding over the verify-node's
    output. Returns ``None`` to REGENERATE (the caller also stops at max_rounds).
    """
    if not result.succeeded:
        return GROUP_FAILED
    output = _evaluate_node_output(run_dir, result.run_id, spec.verify_node)

    def output_of(node_id: str) -> dict[str, Any] | None:
        return output if node_id == spec.verify_node else None

    if evaluate_predicate(spec.accept, output_of):
        return GROUP_ACCEPTED
    return None


def _adversarial_feedback(spec: AdversarialSpec, run_dir: Path, run_id: str) -> object | None:
    """The verifier feedback to feed into the next round, or ``None`` when not declared."""
    if spec.feedback is None:
        return None
    output = _evaluate_node_output(run_dir, run_id, spec.verify_node)
    return _feedback_value(output, spec.feedback.from_field)


def _persist_adversarial_state(
    spec: AdversarialSpec,
    base: Path,
    group_id: str,
    iterations: tuple[IterationResult, ...],
    status: str,
) -> None:
    """Persist the adversarial Run Group's authoritative controller state (ADR 0009)."""
    _write_group_state(base, group_id, spec.model_dump(mode="json"), iterations, status)


async def resume_adversarial_verification(
    group_id: str, base: Path, registry: AdapterRegistry | None = None
) -> GroupResult:
    """Resume an interrupted adversarial-verification Run Group (#17, ADR 0002 / 0009).

    Re-reads ``group.json`` and resumes only a NON-TERMINAL group: ``accepted`` (the
    accept Predicate held) and ``rejected`` (the cap was reached) are terminal and
    refused; ``running`` (interrupted between rounds) and ``failed`` (the last round's
    Run is itself resumable per Resume Eligibility) continue. A SUCCEEDED round Run is
    never re-run; an incomplete last round is ``resume_run``'d in place, then the loop
    continues feeding the last completed round's feedback forward.
    """
    persisted = load_group_state(group_id, base)
    spec = AdversarialSpec.model_validate(persisted["spec"])
    resolved_registry = registry or AdapterRegistry()
    iterations_root = group_iterations_root(group_id, base)

    status = persisted["status"]
    if status in {GROUP_ACCEPTED, GROUP_REJECTED}:
        raise ControllerError(
            f"run group {group_id!r} already finished (status: {status}); nothing to resume"
        )

    completed = [
        IterationResult(
            iteration_index=it["iteration_index"],
            run_id=it["run_id"],
            succeeded=it["succeeded"],
        )
        for it in persisted["iterations"]
    ]

    if completed:
        last = completed[-1]
        last_run_dir = iterations_root / last.run_id
        with StateStore(last_run_dir / "state.sqlite", read_only=True) as state:
            last_status = state.run_status(last.run_id)
        if is_resumable(last_status):
            resumed = await resume_run(last.run_id, iterations_root, resolved_registry)
            completed[-1] = IterationResult(
                iteration_index=last.iteration_index,
                run_id=resumed.run_id,
                succeeded=resumed.succeeded,
            )
            _record_membership(last_run_dir, resumed.run_id, group_id, last.iteration_index)
            verdict = _verification_verdict(spec, last_run_dir, resumed)
            if verdict is not None:
                _persist_adversarial_state(spec, base, group_id, tuple(completed), verdict)
                return GroupResult(
                    group_id=group_id, status=verdict, iterations=tuple(completed)
                )

    feedback_value: object | None = None
    if completed:
        last = completed[-1]
        feedback_value = _adversarial_feedback(spec, iterations_root / last.run_id, last.run_id)

    return await _drive_adversarial(
        spec,
        base,
        group_id,
        resolved_registry,
        iterations=tuple(completed),
        feedback_value=feedback_value,
    )


# ======================================================================================
# Tournament (#17)
# ======================================================================================
#
# A tournament runs candidates in rounds, compares their outputs, promotes the round's
# winner into the next round, and reports the final result with each round's comparison
# evidence. It is a Pattern Controller on the SAME Run Group infrastructure — immutable
# per-round Runs, the membership mirror, the ``group.json`` control state, structural
# substitution (ADR 0009). Each round is one Run whose ``compare_node`` names a winner
# in its ``structured_output``; the Controller promotes that winner (and optionally
# feeds a next-round fixture forward) into the next round, and runs a fixed number of
# rounds. The per-round comparison evidence lives in each round's compare-node output,
# so the aggregate group report (which already carries every node's structured_output)
# surfaces it with no report change; the final winner is recorded on ``group.json``.

# The tournament Run Group's status (#17):
#   TERMINAL: "complete" — every round ran and the final winner was promoted
#   RESUMABLE: "running" (interrupted between rounds), "failed" (a round's Run failed)
GROUP_COMPLETE = "complete"


class PromoteSpec(BaseModel):
    """Where a tournament promotes a round's winner into the next round (#17).

    The winner SOURCE is fixed — ``TournamentSpec.compare_node``'s
    ``structured_output[winner_field]`` — so a PromoteSpec only names the DESTINATION:
    the winner is substituted into ``to_field`` of node ``to_node`` BEFORE the next
    round materializes (structural substitution, ADR 0009).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    to_node: str
    to_field: str


class TournamentSpec(BaseModel):
    """The authored definition of a tournament Run Group (#17).

    ``workflow`` is the round ``Workflow`` file (an ordinary single-iteration graph
    that runs/compares candidates); ``rounds`` is how many rounds to run; ``compare_node``
    is the id of the node whose ``structured_output`` names the round's winner;
    ``winner_field`` is the field of that output carrying the winning candidate;
    ``promote`` (optional) substitutes the winner into the next round's named input;
    ``feedback`` (optional) feeds a next-round value (e.g. the next compare fixture)
    forward, exactly as the other Controllers do.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    workflow: Path
    rounds: int = Field(ge=1)
    compare_node: str
    winner_field: str
    promote: PromoteSpec | None = None
    feedback: FeedbackSpec | None = None


async def run_tournament(
    spec: TournamentSpec, base: Path, registry: AdapterRegistry | None = None
) -> GroupResult:
    """Drive a tournament Run Group to completion (#17, ADR 0002 / 0009).

    Materializes each round as a separate immutable Run, promoting the round's winner
    into the next round, until every round has run (``complete``) or a round's Run
    fails (``failed``). The final round's winner is the tournament's reported result.
    """
    group_id = _new_group_id()
    group_dir(group_id, base).mkdir(parents=True, exist_ok=True)
    return await _drive_tournament(
        spec, base, group_id, registry, iterations=(), promoted=(), feedback_value=None
    )


async def _drive_tournament(
    spec: TournamentSpec,
    base: Path,
    group_id: str,
    registry: AdapterRegistry | None,
    *,
    iterations: tuple[IterationResult, ...],
    promoted: tuple[str | None, ...],
    feedback_value: object | None,
) -> GroupResult:
    """The round loop, shared by a fresh run and a group resume.

    ``promoted`` seeds the already-recorded per-round winners (aligned with
    ``iterations``); ``feedback_value`` seeds the next round's fed-forward value.
    """
    resolved_registry = registry or AdapterRegistry()
    iterations_root = group_iterations_root(group_id, base)
    iterations_root.mkdir(parents=True, exist_ok=True)
    base_raw = load_workflow_file(spec.workflow)
    base_dir = spec.workflow.resolve().parent
    completed = list(iterations)
    winners = list(promoted)
    last_winner = winners[-1] if winners else None

    while len(completed) < spec.rounds:
        index = len(completed)
        round_raw = _materialize_iteration_raw(
            base_raw, _round_substitutions(spec, last_winner, feedback_value)
        )
        workflow = normalize_workflow(
            round_raw, source=f"{spec.workflow} (round {index})", base_dir=base_dir
        )
        if not any(node.id == spec.compare_node for node in workflow.nodes):
            raise ControllerError(
                f"compare_node {spec.compare_node!r} is not in the round workflow"
            )
        result = await execute_run(workflow, iterations_root, resolved_registry)
        run_dir = iterations_root / result.run_id
        _record_membership(run_dir, result.run_id, group_id, index)
        completed.append(
            IterationResult(
                iteration_index=index, run_id=result.run_id, succeeded=result.succeeded
            )
        )

        if not result.succeeded:
            # A failed round has no winner to promote forward: stop the tournament.
            winners.append(None)
            _persist_tournament_state(spec, base, group_id, tuple(completed), tuple(winners), GROUP_FAILED)
            return GroupResult(group_id=group_id, status=GROUP_FAILED, iterations=tuple(completed))

        last_winner = _round_winner(spec, run_dir, result.run_id)
        winners.append(last_winner)
        feedback_value = _tournament_feedback(spec, run_dir, result.run_id)
        status = GROUP_COMPLETE if len(completed) >= spec.rounds else GROUP_RUNNING
        _persist_tournament_state(spec, base, group_id, tuple(completed), tuple(winners), status)

    return GroupResult(
        group_id=group_id,
        status=GROUP_COMPLETE,
        iterations=tuple(completed),
        winner=last_winner,
    )


def _round_substitutions(
    spec: TournamentSpec, winner: str | None, feedback_value: object | None
) -> tuple[_Substitution, ...]:
    """The structural substitutions for a round: the promoted winner + any feedback.

    Round 1 promotes nothing (no prior winner). A later round substitutes the prior
    round's winner into ``promote`` (when declared) AND the fed-forward feedback value
    into ``feedback`` (when declared) — composed into the single substitution primitive.
    """
    substitutions: list[_Substitution] = []
    if spec.promote is not None and winner is not None:
        substitutions.append(
            _Substitution(to_node=spec.promote.to_node, to_field=spec.promote.to_field, value=winner)
        )
    substitutions.extend(_feedback_substitution(spec.feedback, feedback_value))
    return tuple(substitutions)


def _round_winner(spec: TournamentSpec, run_dir: Path, run_id: str) -> str | None:
    """The round's winning candidate: ``compare_node``'s ``structured_output[winner_field]``.

    ``None`` when the compare node produced no structured output or lacks the field, so
    a malformed round simply promotes nothing forward. The value is coerced to ``str``
    (a candidate identifier) when present.
    """
    output = _evaluate_node_output(run_dir, run_id, spec.compare_node)
    if output is None:
        return None
    structured = output.get("structured_output")
    if not isinstance(structured, dict):
        return None
    winner = structured.get(spec.winner_field)
    return None if winner is None else str(winner)


def _tournament_feedback(spec: TournamentSpec, run_dir: Path, run_id: str) -> object | None:
    """The value to feed the next round, or ``None`` when no feedback spec is declared."""
    if spec.feedback is None:
        return None
    output = _evaluate_node_output(run_dir, run_id, spec.compare_node)
    return _feedback_value(output, spec.feedback.from_field)


def _persist_tournament_state(
    spec: TournamentSpec,
    base: Path,
    group_id: str,
    iterations: tuple[IterationResult, ...],
    winners: tuple[str | None, ...],
    status: str,
) -> None:
    """Persist the tournament Run Group's authoritative controller state (ADR 0009).

    Records each round's promoted winner on its iteration entry (the comparison trail)
    and the final winner at the top level, so ``group.json`` is the single queryable
    source of the tournament's result without re-reading every round's output.
    """
    extra_iteration_fields = {
        it.run_id: {"promoted": winners[position]}
        for position, it in enumerate(iterations)
        if position < len(winners)
    }
    final_winner = next(
        (winner for winner in reversed(winners) if winner is not None), None
    )
    _write_group_state(
        base,
        group_id,
        spec.model_dump(mode="json"),
        iterations,
        status,
        extra_iteration_fields=extra_iteration_fields,
        extra={"winner": final_winner},
    )


async def resume_tournament(
    group_id: str, base: Path, registry: AdapterRegistry | None = None
) -> GroupResult:
    """Resume an interrupted tournament Run Group (#17, ADR 0002 / 0009).

    Re-reads ``group.json`` and resumes only a NON-TERMINAL group: ``complete`` (every
    round ran) is terminal and refused; ``running`` (interrupted between rounds) and
    ``failed`` (the last round's Run is itself resumable per Resume Eligibility)
    continue. A SUCCEEDED round Run is never re-run; an incomplete last round is
    ``resume_run``'d in place, then the loop continues promoting the last winner forward.
    """
    persisted = load_group_state(group_id, base)
    spec = TournamentSpec.model_validate(persisted["spec"])
    resolved_registry = registry or AdapterRegistry()
    iterations_root = group_iterations_root(group_id, base)

    status = persisted["status"]
    if status == GROUP_COMPLETE:
        raise ControllerError(
            f"run group {group_id!r} already finished (status: {status}); nothing to resume"
        )

    completed = [
        IterationResult(
            iteration_index=it["iteration_index"],
            run_id=it["run_id"],
            succeeded=it["succeeded"],
        )
        for it in persisted["iterations"]
    ]
    winners: list[str | None] = [it.get("promoted") for it in persisted["iterations"]]

    if completed:
        last = completed[-1]
        last_run_dir = iterations_root / last.run_id
        with StateStore(last_run_dir / "state.sqlite", read_only=True) as state:
            last_status = state.run_status(last.run_id)
        if is_resumable(last_status):
            resumed = await resume_run(last.run_id, iterations_root, resolved_registry)
            completed[-1] = IterationResult(
                iteration_index=last.iteration_index,
                run_id=resumed.run_id,
                succeeded=resumed.succeeded,
            )
            _record_membership(last_run_dir, resumed.run_id, group_id, last.iteration_index)
            if not resumed.succeeded:
                if winners:
                    winners[-1] = None
                _persist_tournament_state(
                    spec, base, group_id, tuple(completed), tuple(winners), GROUP_FAILED
                )
                return GroupResult(
                    group_id=group_id, status=GROUP_FAILED, iterations=tuple(completed)
                )
            if winners:
                winners[-1] = _round_winner(spec, last_run_dir, resumed.run_id)

    feedback_value: object | None = None
    if completed and completed[-1].succeeded:
        last = completed[-1]
        feedback_value = _tournament_feedback(spec, iterations_root / last.run_id, last.run_id)

    return await _drive_tournament(
        spec,
        base,
        group_id,
        resolved_registry,
        iterations=tuple(completed),
        promoted=tuple(winners),
        feedback_value=feedback_value,
    )
