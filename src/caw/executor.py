"""Execute one Run of a normalized Workflow on the local Engine Backend (ADR 0003)."""

import asyncio
import contextlib
import json
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from caw.events import EventLog
from caw.model import Node, Workflow, definition_checksum, execution_order, workflow_snapshot
from caw.state import StateStore


@dataclass(frozen=True)
class NodeResult:
    """The normalized output of one Node Attempt."""

    node_id: str
    exit_status: int
    stdout: str
    stderr: str
    started_at: str
    finished_at: str

    @property
    def succeeded(self) -> bool:
        return self.exit_status == 0

    @property
    def status(self) -> str:
        return "succeeded" if self.succeeded else "failed"

    @property
    def normalized_output(self) -> dict[str, Any]:
        return {"exit_status": self.exit_status, "stdout": self.stdout, "stderr": self.stderr}


@dataclass(frozen=True)
class RunResult:
    """The outcome of one Run.

    ``node_results`` holds every attempted Node; ``skipped_node_ids`` names the
    transitive dependents of a failed Node that were never attempted (#4). A Run
    fails if any attempted Node failed. A failure does not always coincide with a
    skip: a failed LEAF Node has no dependents to skip, so the Run fails with
    ``skipped_node_ids`` empty.

    ``skipped_blockers`` maps each skipped Node id to the failed Node that blocked
    it, so a Reporter can tell a user which downstream work was withheld and why.
    """

    run_id: str
    node_results: tuple[NodeResult, ...]
    skipped_node_ids: tuple[str, ...] = ()
    skipped_blockers: Mapping[str, str] = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        # A skipped Node is always the transitive dependent of a failed Node, so
        # an all-succeeded `node_results` already implies nothing was skipped; the
        # failed Node that caused the skip is itself in `node_results`.
        return all(result.succeeded for result in self.node_results)

    @property
    def status(self) -> str:
        return "succeeded" if self.succeeded else "failed"


def _new_run_id() -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{secrets.token_hex(4)}"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _task_crash(task: "asyncio.Task[NodeResult]") -> BaseException:
    """The exception that crashed a completed task: its error, or CancelledError."""
    if task.cancelled():
        return asyncio.CancelledError()
    exception = task.exception()
    assert exception is not None, "caller guarantees the task raised"
    return exception


async def _execute_shell_node(node: Node) -> NodeResult:
    started_at = _now()
    process = await asyncio.create_subprocess_shell(
        node.inputs.command,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await process.communicate()
    except asyncio.CancelledError:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
        await process.wait()
        raise
    exit_status = process.returncode if process.returncode is not None else -1
    return NodeResult(
        node_id=node.id,
        exit_status=exit_status,
        stdout=stdout.decode(errors="backslashreplace"),
        stderr=stderr.decode(errors="backslashreplace"),
        started_at=started_at,
        finished_at=_now(),
    )


def _finalize_crashed_run(
    state: StateStore,
    events: EventLog,
    run_id: str,
    in_flight_node_ids: tuple[str, ...],
    error: str,
) -> None:
    """Best-effort finalization of a crashed Run; never masks the original exception.

    Suppresses BaseException, not just Exception: a second KeyboardInterrupt or
    SystemExit arriving mid-finalization must not replace the crash being reported.

    Every Node still in flight when the Run crashed is marked ``errored`` so no
    Node is left recorded as ``running``. The ``run_errored`` event names every
    one of those Nodes so the Event trace and State agree on the crash's blast
    radius for a multi-node concurrent crash (#54).
    """
    for node_id in in_flight_node_ids:
        with contextlib.suppress(BaseException):
            state.record_node_finished(run_id=run_id, node_id=node_id, status="errored")
    with contextlib.suppress(BaseException):
        state.record_run_errored(run_id=run_id, error=error, finished_at=_now())
    with contextlib.suppress(BaseException):
        events.append("run_errored", {"error": error, "node_ids": list(in_flight_node_ids)})


class _Scheduler:
    """A readiness-based scheduler over one Run's acyclic graph (ADR 0003).

    A Node becomes ready once all the Nodes it ``needs`` have SUCCEEDED. Ready
    Nodes are launched as asyncio tasks — one task per Node Attempt — up to the
    workflow's concurrency limit; as each task completes the scheduler
    re-evaluates readiness. A join (a Node with multiple needs) waits for all of
    them to succeed, which falls out of readiness for free.

    Failure semantics (#4): when a Node fails, its transitive dependents are
    marked ``skipped`` and never attempted, while independent ready Nodes keep
    running and Nodes already in flight are left to finish — the scheduler never
    cancels a running sibling on a peer's failure. The Run's final status is
    ``failed`` if any Node failed (equivalently, if any Node was skipped).
    """

    def __init__(
        self, workflow: Workflow, state: StateStore, events: EventLog, run_id: str
    ) -> None:
        self._state = state
        self._events = events
        self._run_id = run_id
        self._concurrency = workflow.concurrency
        # execution_order seeds a deterministic launch order among ready Nodes:
        # declaration-order tie-break, the same order `caw graph` reports.
        self._ordered = execution_order(workflow)
        self._dependents: dict[str, list[str]] = {node.id: [] for node in self._ordered}
        self._indegree: dict[str, int] = {}
        for node in self._ordered:
            self._indegree[node.id] = len(node.needs)
            for need in node.needs:
                self._dependents[need].append(node.id)
        self._in_flight: dict[asyncio.Task[NodeResult], Node] = {}
        self._results: list[NodeResult] = []
        self._skipped: list[str] = []
        self._skipped_blockers: dict[str, str] = {}

    @property
    def in_flight_node_ids(self) -> tuple[str, ...]:
        """The ids of Nodes whose Attempts are in flight, for crash finalization."""
        return tuple(node.id for node in self._in_flight.values())

    def _ready_nodes(self) -> list[Node]:
        """Nodes whose needs are all satisfied and that are neither running nor done."""
        running = {node.id for node in self._in_flight.values()}
        done = {result.node_id for result in self._results} | set(self._skipped)
        return [
            node
            for node in self._ordered
            if self._indegree[node.id] == 0 and node.id not in running and node.id not in done
        ]

    def _launch_ready(self) -> None:
        for node in self._ready_nodes():
            if len(self._in_flight) >= self._concurrency:
                break
            self._state.record_node_started(run_id=self._run_id, node_id=node.id)
            self._events.append("node_started", {"node_id": node.id, "attempt": 1})
            task = asyncio.ensure_future(_execute_shell_node(node))
            self._in_flight[task] = node

    def _record_finished(self, node: Node, result: NodeResult) -> None:
        self._state.record_attempt(
            run_id=self._run_id,
            node_id=node.id,
            attempt=1,
            started_at=result.started_at,
            finished_at=result.finished_at,
            exit_status=result.exit_status,
            output=result.normalized_output,
        )
        self._state.record_node_finished(
            run_id=self._run_id, node_id=node.id, status=result.status
        )
        self._events.append(
            "node_finished",
            {
                "node_id": node.id,
                "attempt": 1,
                "exit_status": result.exit_status,
                "status": result.status,
            },
        )

    def _on_success(self, node: Node) -> None:
        for dependent in self._dependents[node.id]:
            self._indegree[dependent] -= 1

    def _skip_transitive_dependents(self, node: Node) -> None:
        """Mark every not-yet-attempted transitive dependent of a failed Node skipped.

        Independent branches are untouched: only Nodes reachable from the failed
        Node by ``needs`` edges are skipped, recorded ``skipped`` in both State
        and Events so they are distinguishable from Nodes that ran.
        """
        already = {result.node_id for result in self._results} | set(self._skipped)
        queue = list(self._dependents[node.id])
        seen: set[str] = set()
        while queue:
            node_id = queue.pop()
            if node_id in seen or node_id in already:
                continue
            seen.add(node_id)
            self._skipped.append(node_id)
            self._skipped_blockers[node_id] = node.id
            self._state.record_node_skipped(run_id=self._run_id, node_id=node_id)
            self._events.append("node_skipped", {"node_id": node_id, "blocked_by": node.id})
            queue.extend(self._dependents[node_id])

    async def run(self) -> RunResult:
        self._launch_ready()
        try:
            while self._in_flight:
                done, _ = await asyncio.wait(
                    self._in_flight.keys(), return_when=asyncio.FIRST_COMPLETED
                )
                # Record every completed task in the batch before propagating any
                # raise. A task that raised (e.g. a subprocess that could not be
                # spawned, or cancellation) crashes the whole Run, but a PEER in
                # the same batch that finished with a non-zero exit is an ordinary
                # Node failure whose transitive dependents must still be skipped:
                # processing the whole batch first keeps the failed-node-skips-its-
                # dependents invariant intact even on the crash path (#54). The
                # first raised task's exception is re-raised after the batch is
                # fully recorded so execute_run can finalize the crash.
                crash: BaseException | None = None
                for task in done:
                    node = self._in_flight[task]
                    if task.cancelled() or task.exception() is not None:
                        crash = crash or _task_crash(task)
                        continue
                    self._in_flight.pop(task)
                    result = task.result()
                    self._record_finished(node, result)
                    self._results.append(result)
                    if result.succeeded:
                        self._on_success(node)
                    else:
                        self._skip_transitive_dependents(node)
                if crash is not None:
                    raise crash
                self._launch_ready()
        except BaseException:
            await self._drain_in_flight()
            raise
        return RunResult(
            run_id=self._run_id,
            node_results=tuple(self._results),
            skipped_node_ids=tuple(self._skipped),
            skipped_blockers=dict(self._skipped_blockers),
        )

    async def _drain_in_flight(self) -> None:
        """Tear down sibling Attempts after a crash so no subprocess is orphaned.

        A sibling Attempt that had already finished is recorded by its real
        result, so a crash does not erase a peer's completed work. Any still
        running is cancelled and awaited, which routes through the node runner's
        cancellation handler to kill its subprocess; those tasks stay in flight
        and are finalized as ``errored`` by execute_run.
        """
        for task, node in list(self._in_flight.items()):
            if task.done() and not task.cancelled() and task.exception() is None:
                self._in_flight.pop(task, None)
                self._record_finished(node, task.result())
                self._results.append(task.result())
        for task in list(self._in_flight):
            task.cancel()
        for task in list(self._in_flight):
            with contextlib.suppress(BaseException):
                await task


async def execute_run(workflow: Workflow, runs_root: Path) -> RunResult:
    """Materialize a run directory, execute the Workflow's Nodes, and persist the Run."""
    # A normally constructed Workflow is validated `concurrency >= 1`; a Workflow
    # that bypassed validation (model_construct, model_copy(update=...)) could
    # reach the scheduler with concurrency < 1, which would launch nothing and
    # report a vacuous `succeeded`. Fail loud before any run-directory side
    # effects, mirroring the ordering layer's bypass guard (model.execution_order).
    if workflow.concurrency < 1:
        raise ValueError(
            f"workflow {workflow.name!r} has concurrency {workflow.concurrency} "
            f"(must be >= 1; was validation bypassed?)"
        )
    run_id = _new_run_id()
    run_dir = runs_root / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "workflow.normalized.json").write_text(
        json.dumps(workflow_snapshot(workflow), indent=2) + "\n", encoding="utf-8"
    )
    events = EventLog(run_dir / "events.jsonl", run_id=run_id)

    with StateStore(run_dir / "state.sqlite") as state:
        state.record_run_started(
            run_id=run_id,
            workflow_name=workflow.name,
            definition_checksum=definition_checksum(workflow),
            created_at=_now(),
        )
        events.append("run_started", {"workflow_name": workflow.name})
        scheduler = _Scheduler(workflow, state, events, run_id)
        try:
            run_result = await scheduler.run()
            state.record_run_finished(run_id=run_id, status=run_result.status, finished_at=_now())
            events.append("run_finished", {"status": run_result.status})
        except BaseException as exc:
            message = str(exc)
            error = f"{type(exc).__name__}: {message}" if message else type(exc).__name__
            _finalize_crashed_run(state, events, run_id, scheduler.in_flight_node_ids, error)
            raise
    return run_result
