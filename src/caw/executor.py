"""Execute one Run of a normalized Workflow on the local Engine Backend (ADR 0003)."""

import asyncio
import contextlib
import json
import os
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from caw.adapter import AdapterRegistry, AgentInvocation
from caw.contract import OutputContractError, validate_output_contract
from caw.events import EventLog
from caw.model import (
    AgentNodeInputs,
    Node,
    ShellNodeInputs,
    Workflow,
    definition_checksum,
    execution_order,
    workflow_snapshot,
)
from caw.state import StateStore

# Error classification (#6): the terminal status of a failed Node Attempt names
# WHY it failed, so a timeout is diagnosable as a timeout and an adapter/internal
# error is distinguishable from a node that ran and exited non-zero. The kinds:
#   "failed"    — the runner ran and reported a non-zero exit status
#   "timed_out" — the Attempt exceeded the Node's wall-clock `timeout` budget and
#                 was terminated (subprocess killed)
#   "errored"   — an Adapter or internal exception prevented the runner from
#                 producing a result at all
# ``failure_kind is None`` is the single source of truth for success, so the
# status taxonomy stays in one place rather than scattered across call sites.
FAILED = "failed"
TIMED_OUT = "timed_out"
ERRORED = "errored"

# The failure kinds the executor RE-ATTEMPTS when a Node has retries remaining
# (#6). A non-zero exit and a timeout are commonly transient (a flaky command, a
# slow upstream), so they are retryable; an ``errored`` failure is an
# Adapter/internal fault (unknown adapter, unreadable fixture, a bug) that is
# almost always deterministic, so retrying it would only burn Attempts — it goes
# terminal immediately. No backoff is applied between Attempts in v0.1.
_RETRYABLE_FAILURE_KINDS = frozenset({FAILED, TIMED_OUT})


@dataclass(frozen=True)
class NodeResult:
    """The normalized output of one Node Attempt.

    Shell and agent Nodes share this shape so the scheduler, State, and Events
    treat them identically. ``structured_output`` and ``artifacts`` are populated
    only for agent Nodes (the Adapter's normalized result); for shell Nodes they
    stay ``None``/empty. ``artifacts`` holds durable file paths produced by the
    Attempt, indexed minimally in State (#5).

    ``failure_kind`` classifies a failed Attempt (#6): ``None`` means the Attempt
    succeeded; otherwise it is one of ``FAILED`` / ``TIMED_OUT`` / ``ERRORED`` and
    becomes the Node's terminal status. It is the single source of truth for
    success so a timeout (exit_status -1, ``TIMED_OUT``) is never read as an
    ordinary non-zero exit.
    """

    node_id: str
    exit_status: int
    stdout: str
    stderr: str
    started_at: str
    finished_at: str
    structured_output: object | None = None
    artifacts: tuple[Path, ...] = ()
    failure_kind: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.failure_kind is None

    @property
    def status(self) -> str:
        # succeeded ⇔ failure_kind is None, so a non-success always carries a
        # concrete kind string; assert keeps the return type str for mypy.
        if self.failure_kind is None:
            return "succeeded"
        return self.failure_kind

    @property
    def retryable(self) -> bool:
        """Whether this failure kind is worth re-attempting if retries remain (#6)."""
        return self.failure_kind in _RETRYABLE_FAILURE_KINDS

    @property
    def normalized_output(self) -> dict[str, Any]:
        output: dict[str, Any] = {
            "exit_status": self.exit_status,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }
        # Only agent Nodes carry structured output / artifacts; omit them for
        # shell Nodes so the persisted output shape is unchanged from before #5.
        if self.structured_output is not None:
            output["structured_output"] = self.structured_output
        if self.artifacts:
            output["artifacts"] = [str(path) for path in self.artifacts]
        return output


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


async def _kill_and_reap(process: "asyncio.subprocess.Process") -> None:
    """Kill a still-running subprocess and reap it so no orphan is left behind.

    Shared by the cancellation and timeout paths (#6): a node whose budget
    expires must leave no live subprocess, exactly as cancellation does. A
    process that already exited (returncode set) needs no kill; ProcessLookupError
    is suppressed for the race where it exits between the check and the signal.
    """
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
    await process.wait()


async def _execute_shell_node(node: Node) -> NodeResult:
    """Run a shell Node's command as a subprocess, enforcing its timeout budget.

    A non-zero exit is an ordinary node failure (``FAILED``). A Node that exceeds
    its ``timeout`` is terminated — the subprocess is KILLED so no orphan is left,
    mirroring the cancellation handler — and classified ``TIMED_OUT`` with
    exit_status -1, so a timeout is never read as a non-zero exit (#6). Timeout
    wraps ``communicate()`` (the wall-clock the node spends running); a Node with
    no ``timeout`` runs unbounded as before.
    """
    assert isinstance(node.inputs, ShellNodeInputs)
    started_at = _now()
    process = await asyncio.create_subprocess_shell(
        node.inputs.command,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(node.timeout):
            stdout, stderr = await process.communicate()
    except TimeoutError:
        await _kill_and_reap(process)
        return NodeResult(
            node_id=node.id,
            exit_status=-1,
            stdout="",
            stderr=f"node {node.id!r} exceeded its timeout of {node.timeout}s",
            started_at=started_at,
            finished_at=_now(),
            failure_kind=TIMED_OUT,
        )
    except asyncio.CancelledError:
        await _kill_and_reap(process)
        raise
    exit_status = process.returncode if process.returncode is not None else -1
    return NodeResult(
        node_id=node.id,
        exit_status=exit_status,
        stdout=stdout.decode(errors="backslashreplace"),
        stderr=stderr.decode(errors="backslashreplace"),
        started_at=started_at,
        finished_at=_now(),
        failure_kind=None if exit_status == 0 else FAILED,
    )


def _resolve_declared_env(declared: tuple[str, ...]) -> dict[str, str]:
    """Resolve declared env var NAMES to their values from the parent environment.

    The env policy is allow-list-only: a Node receives a variable solely if it
    declared the name AND that name is present in the parent environment. Nothing
    else from the parent environment passes through, and an undeclared or absent
    name is simply omitted — never defaulted. The returned mapping is the ONLY
    environment the Adapter (and thus the Agent CLI process) sees for this Node;
    its VALUES are never persisted to State, Events, or the snapshot (#5).
    """
    return {name: os.environ[name] for name in declared if name in os.environ}


async def _execute_agent_node(node: Node, registry: AdapterRegistry) -> NodeResult:
    """Run an agent Node through its Adapter and validate its Output Contract.

    Adapter or Output-Contract failures are recorded as an ordinary node failure
    (exit_status 1) with the cause on stderr, so the #4 scheduler skips the
    failed Node's dependents exactly as it does for a non-zero shell Node. The
    Output Contract is validated AFTER the Adapter returns and BEFORE the result
    is reported, so a contract breach fails the Node even when the Agent CLI
    itself exited zero.

    Exit-status gating (#63): the Output Contract is evaluated ONLY when the Agent
    CLI exited zero. The contract is a guarantee about a successful invocation's
    structured output; a non-zero exit is already a node failure, so re-checking
    the contract would be redundant and could mask the agent's own failure cause
    with a contract message. The structured output is validated as-is, including
    JSON null — a schema permitting null passes, one requiring content fails — so
    the schema is the sole arbiter and None is never special-cased.
    """
    assert isinstance(node.inputs, AgentNodeInputs)
    inputs = node.inputs
    started_at = _now()
    adapter = registry.resolve(inputs.adapter)
    invocation = AgentInvocation(
        node_id=node.id,
        adapter=inputs.adapter,
        prompt=inputs.prompt,
        args=inputs.args,
        env=_resolve_declared_env(inputs.env),
        output_schema=inputs.output_schema,
        fixture=inputs.fixture,
    )
    # The timeout wraps the Adapter invocation — the wall-clock the node spends
    # waiting on the external Agent CLI (#6). A TimeoutError is classified
    # TIMED_OUT here rather than caught by the generic ERRORED handler in
    # _execute_node, so a slow agent is diagnosable as a timeout, not an error.
    try:
        async with asyncio.timeout(node.timeout):
            result = await adapter.invoke(invocation)
    except TimeoutError:
        return NodeResult(
            node_id=node.id,
            exit_status=-1,
            stdout="",
            stderr=f"node {node.id!r} (adapter {inputs.adapter!r}) "
            f"exceeded its timeout of {node.timeout}s",
            started_at=started_at,
            finished_at=_now(),
            failure_kind=TIMED_OUT,
        )
    exit_status = result.exit_status
    stderr = result.stderr
    if exit_status == 0 and inputs.output_schema is not None:
        try:
            validate_output_contract(inputs.output_schema, result.structured_output)
        except OutputContractError as exc:
            exit_status = 1
            stderr = f"{stderr}\n{exc}".strip() if stderr else str(exc)
    return NodeResult(
        node_id=node.id,
        exit_status=exit_status,
        stdout=result.stdout,
        stderr=stderr,
        started_at=started_at,
        finished_at=_now(),
        structured_output=result.structured_output,
        artifacts=result.artifacts,
        failure_kind=None if exit_status == 0 else FAILED,
    )


async def _execute_node(node: Node, registry: AdapterRegistry) -> NodeResult:
    """Dispatch a Node Attempt to the runner for its kind.

    The single dispatch seam every new Node kind extends: shell Nodes spawn a
    subprocess, agent Nodes go through an Adapter. An Adapter-level failure
    (unknown adapter, unreadable fixture) is normalized into a failed NodeResult
    here so the scheduler treats it like any other node failure rather than a Run
    crash. Both runners share one NodeResult shape, so State, Events, and the
    scheduler stay kind-agnostic.
    """
    if isinstance(node.inputs, ShellNodeInputs):
        return await _execute_shell_node(node)
    try:
        return await _execute_agent_node(node, registry)
    except Exception as exc:
        # ANY Exception from the agent path — an AdapterError (unknown adapter,
        # unreadable/malformed fixture), an OutputContractError, or an arbitrary
        # exception a real Agent CLI Adapter raises inside invoke() (parse,
        # subprocess, timeout) — is normalized into a failed Node here so the
        # scheduler skips its dependents uniformly rather than the exception
        # escaping and crashing the whole Run (#61, ADR 0006's own contract).
        #
        # Only Exception is caught, never BaseException: asyncio.CancelledError
        # (a BaseException since 3.8), KeyboardInterrupt, and SystemExit must
        # still propagate so the #4 crash/cancel path and #22/#54 finalization
        # tear down siblings and record the Run errored.
        now = _now()
        cause = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
        return NodeResult(
            node_id=node.id,
            exit_status=1,
            stdout="",
            stderr=cause,
            started_at=now,
            finished_at=now,
            failure_kind=ERRORED,
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
        self,
        workflow: Workflow,
        state: StateStore,
        events: EventLog,
        run_id: str,
        registry: AdapterRegistry,
        *,
        satisfied_seed: Mapping[str, str] | None = None,
        attempt_seed: Mapping[str, int] | None = None,
        started_seed: set[str] | None = None,
    ) -> None:
        self._state = state
        self._events = events
        self._run_id = run_id
        self._registry = registry
        self._concurrency = workflow.concurrency
        self._by_id: dict[str, Node] = {node.id: node for node in workflow.nodes}
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
        # Per-Node Attempt bookkeeping for the in-run retry loop (#6). ``_attempt``
        # is the Attempt NUMBER the next launch of a Node uses, so re-launched
        # Nodes write distinct ``attempt`` rows ((run_id, node_id, attempt) is the
        # State PK). ``_started`` records which Nodes already have a ``node`` row,
        # so a retry re-launch does not re-INSERT it. The numbering and the
        # node-row seed are overridable so a resume continues past the Attempts a
        # prior run already recorded, never colliding with them.
        self._attempt: dict[str, int] = dict(attempt_seed or {})
        self._started: set[str] = set(started_seed or ())
        # A resume seeds the Nodes that already SUCCEEDED in the prior Run so they
        # are not re-run, yet their dependents become ready (#6). Decrementing
        # indegree for each satisfied Node's dependents mirrors the on-success
        # path; the satisfied set is treated as ``done`` by readiness so the Node
        # itself is never launched again. The seed maps node_id -> its succeeded
        # status, which becomes its NodeResult status in the resumed RunResult.
        self._satisfied: dict[str, str] = dict(satisfied_seed or {})
        for satisfied_id in self._satisfied:
            for dependent in self._dependents.get(satisfied_id, []):
                self._indegree[dependent] -= 1

    @property
    def in_flight_node_ids(self) -> tuple[str, ...]:
        """The ids of Nodes whose Attempts are in flight, for crash finalization."""
        return tuple(node.id for node in self._in_flight.values())

    def _ready_nodes(self) -> list[Node]:
        """Nodes whose needs are all satisfied and that are neither running nor done."""
        running = {node.id for node in self._in_flight.values()}
        done = (
            {result.node_id for result in self._results}
            | set(self._skipped)
            | set(self._satisfied)
        )
        return [
            node
            for node in self._ordered
            if self._indegree[node.id] == 0 and node.id not in running and node.id not in done
        ]

    def _launch_ready(self) -> None:
        for node in self._ready_nodes():
            if len(self._in_flight) >= self._concurrency:
                break
            attempt = self._attempt.setdefault(node.id, 1)
            # The ``node`` row is INSERTed once; a retry re-launch (and a resume
            # re-run, whose row already exists) flips the existing row back to
            # ``running`` instead, so the PK is never violated.
            if node.id in self._started:
                self._state.record_node_running(run_id=self._run_id, node_id=node.id)
            else:
                self._state.record_node_started(run_id=self._run_id, node_id=node.id)
                self._started.add(node.id)
            self._events.append("node_started", {"node_id": node.id, "attempt": attempt})
            task = asyncio.ensure_future(_execute_node(node, self._registry))
            self._in_flight[task] = node

    def _record_attempt(self, node: Node, result: NodeResult) -> None:
        """Record one Attempt's outcome in State and the Event trace.

        Always written, on every Attempt (the durable Attempt history #6.1), with
        the Node's current Attempt number — distinct from a re-launch's so the
        ``attempt`` PK never collides.
        """
        attempt = self._attempt[node.id]
        self._state.record_attempt(
            run_id=self._run_id,
            node_id=node.id,
            attempt=attempt,
            started_at=result.started_at,
            finished_at=result.finished_at,
            exit_status=result.exit_status,
            output=result.normalized_output,
        )
        self._events.append(
            "node_finished",
            {
                "node_id": node.id,
                "attempt": attempt,
                "exit_status": result.exit_status,
                "status": result.status,
            },
        )

    def _record_finished(self, node: Node, result: NodeResult) -> None:
        """Record an Attempt AND drive the Node to its terminal status.

        The crash-drain path (#54) finalizes a peer that completed mid-crash with
        its real result; the in-run retry loop instead routes through
        ``_handle_result`` so a retryable failure does not go terminal.
        """
        self._record_attempt(node, result)
        self._state.record_node_finished(
            run_id=self._run_id, node_id=node.id, status=result.status
        )

    def _retries_remaining(self, node: Node) -> bool:
        """Whether the Node still has Attempts left under its ``retries`` budget (#6)."""
        return self._attempt[node.id] <= node.retries

    def _handle_result(self, node: Node, result: NodeResult) -> None:
        """Process one finished Attempt: succeed, retry, or fail terminally (#6).

        A succeeded Attempt unblocks dependents. A retryable failure (non-zero
        exit or timeout) with Attempts remaining is recorded and the Node is
        re-queued at the next Attempt number — its dependents are NOT skipped. A
        non-retryable failure, or a retryable one with the budget exhausted, is
        recorded terminal and skips the Node's transitive dependents, preserving
        the #4 branch-failure semantics exactly.
        """
        self._record_attempt(node, result)
        if result.succeeded:
            self._state.record_node_finished(
                run_id=self._run_id, node_id=node.id, status=result.status
            )
            self._results.append(result)
            self._on_success(node)
            return
        if result.retryable and self._retries_remaining(node):
            self._attempt[node.id] += 1
            self._events.append(
                "node_retrying",
                {
                    "node_id": node.id,
                    "next_attempt": self._attempt[node.id],
                    "failure_kind": result.failure_kind,
                },
            )
            return
        self._state.record_node_finished(
            run_id=self._run_id, node_id=node.id, status=result.status
        )
        self._results.append(result)
        self._skip_transitive_dependents(node)

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
                    self._handle_result(node, task.result())
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


async def execute_run(
    workflow: Workflow, runs_root: Path, registry: AdapterRegistry | None = None
) -> RunResult:
    """Materialize a run directory, execute the Workflow's Nodes, and persist the Run.

    ``registry`` resolves agent Nodes' ``adapter`` names to Adapters; it defaults
    to the mock-Adapter registry so shell-only Runs and offline agent Runs need
    no wiring. Real-CLI Adapters (#9, #11) are injected by passing a populated
    registry.
    """
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
        scheduler = _Scheduler(workflow, state, events, run_id, registry or AdapterRegistry())
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
