"""Pattern Controllers: sequence immutable Runs into a Run Group (ADR 0002 / 0009).

A **Pattern Controller** expresses iterative behavior by evaluating a finished Run
and materializing the next one; the kernel itself only executes acyclic Runs
(CONTEXT.md, ADR 0002). This module realizes the first Controller, ``loop_until_done``:
it drives the EXISTING ``execute_run`` / ``resume_run`` as black boxes, materializing
each iteration as a separate immutable Run under a Run Group, and stops on done /
failure / max-iterations.

The Controller is NOT in the IR and is NOT an Expander (ADR 0008): it lives above the
executor, in Python. The loop is described by a :class:`ControllerSpec` (a separate
authored surface from the iteration ``Workflow``), the stop condition reuses the
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

from pydantic import BaseModel, ConfigDict, Field

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

# The Run Group's terminal status: why the loop stopped (ADR 0009).
#   "done"      — the done-predicate held over the finished iteration's output
#   "exhausted" — the iteration index reached max_iterations without done
#   "failed"    — an iteration's Run failed (a failed result is not fed forward)
GROUP_DONE = "done"
GROUP_EXHAUSTED = "exhausted"
GROUP_FAILED = "failed"


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
    graph); ``max_iterations`` is the hard cap; ``done`` is the structured stop
    Predicate (the ``when`` algebra) evaluated against ``evaluate_node``'s output;
    ``feedback`` (optional) carries the prior iteration's output into the next.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    workflow: Path
    max_iterations: int = Field(ge=1)
    # The id of the Run's node whose normalized output the done-predicate and the
    # feedback source read. A Run can have several leaf nodes, so the spec names the
    # one carrying the iteration's verdict explicitly, mirroring a `when` ref's node.
    evaluate_node: str
    done: Predicate
    feedback: FeedbackSpec | None = None


@dataclass(frozen=True)
class IterationResult:
    """One iteration of a Run Group: its run id and whether its Run succeeded."""

    iteration_index: int
    run_id: str
    succeeded: bool


@dataclass(frozen=True)
class GroupResult:
    """The outcome of one ``loop_until_done`` Run Group execution.

    ``status`` is why the loop stopped (``done`` / ``exhausted`` / ``failed``);
    ``iterations`` lists every materialized iteration in order.
    """

    group_id: str
    status: str
    iterations: tuple[IterationResult, ...]


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


def _materialize_iteration_raw(
    base_raw: dict[str, Any], spec: ControllerSpec, feedback_value: object | None
) -> dict[str, Any]:
    """Build iteration N's raw workflow mapping, substituting feedback structurally.

    Iteration 1 (``feedback_value is None``) is the base workflow unchanged. A later
    iteration replaces ``spec.feedback.to_field`` of node ``spec.feedback.to_node``
    with the fed-forward value, so the materialized run carries the prior iteration's
    feedback BEFORE it is normalized and frozen (ADR 0009). With no ``feedback`` spec,
    the workflow is reused unchanged (a pure idempotent loop).
    """
    if spec.feedback is None or feedback_value is None:
        return base_raw
    feedback = spec.feedback
    nodes = base_raw.get("nodes")
    if not isinstance(nodes, list):
        raise ControllerError(
            "feedback requires a `nodes:` iteration workflow; "
            "a `pattern:` workflow cannot carry feedback in v0.1"
        )
    substituted_nodes: list[Any] = []
    found = False
    for node in nodes:
        if isinstance(node, dict) and node.get("id") == feedback.to_node:
            found = True
            inputs = dict(node.get("inputs", {}))
            inputs[feedback.to_field] = feedback_value
            substituted_nodes.append({**node, "inputs": inputs})
        else:
            substituted_nodes.append(node)
    if not found:
        raise ControllerError(
            f"feedback target node {feedback.to_node!r} is not in the iteration workflow"
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
    materializes iterations until the done-predicate holds, an iteration fails, or
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
        iteration_raw = _materialize_iteration_raw(base_raw, spec, feedback_value)
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
        # forward and persist the in-progress group state before the next run.
        feedback_value = _next_feedback(spec, run_dir, result.run_id)
        _persist_group_state(spec, base, group_id, tuple(completed), GROUP_EXHAUSTED)

    _persist_group_state(spec, base, group_id, tuple(completed), GROUP_EXHAUSTED)
    return GroupResult(group_id=group_id, status=GROUP_EXHAUSTED, iterations=tuple(completed))


def _iteration_verdict(
    spec: ControllerSpec, run_dir: Path, result: RunResult, index: int
) -> str | None:
    """The terminal group status if the loop should stop after this iteration, else None.

    Stops on a failed Run, or on the done-predicate holding over the evaluate-node's
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
    """Persist the Run Group's authoritative controller state to ``group.json``.

    Records the group id, the spec, the ordered iterations (run id + outcome), the
    iteration index, and the group status — the resumable source of truth (ADR 0002).
    """
    state = {
        "group_id": group_id,
        "spec": spec.model_dump(mode="json"),
        "iteration_index": len(iterations),
        "status": status,
        "iterations": [
            {
                "iteration_index": it.iteration_index,
                "run_id": it.run_id,
                "succeeded": it.succeeded,
            }
            for it in iterations
        ],
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

    Re-reads ``group.json``; a SUCCEEDED iteration Run is never re-run (Resume
    Eligibility, CONTEXT.md). If the last recorded iteration's Run is incomplete it
    is ``resume_run``'d in place, then the loop continues from the persisted
    iteration index with feedback fed forward from the last completed iteration. A
    group that already finished (done/failed/exhausted with nothing left) is refused.
    """
    persisted = load_group_state(group_id, base)
    spec = ControllerSpec.model_validate(persisted["spec"])
    resolved_registry = registry or AdapterRegistry()
    iterations_root = group_iterations_root(group_id, base)

    if persisted["status"] == GROUP_DONE:
        raise ControllerError(
            f"run group {group_id!r} already finished (status: {persisted['status']}); "
            f"nothing to resume"
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
