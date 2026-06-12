"""Execute one Run of a normalized Workflow on the local Engine Backend (ADR 0003)."""

import asyncio
import contextlib
import json
import secrets
from dataclasses import dataclass
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
    """The outcome of one Run."""

    run_id: str
    node_results: tuple[NodeResult, ...]

    @property
    def succeeded(self) -> bool:
        return all(result.succeeded for result in self.node_results)

    @property
    def status(self) -> str:
        return "succeeded" if self.succeeded else "failed"


def _new_run_id() -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{secrets.token_hex(4)}"


def _now() -> str:
    return datetime.now(UTC).isoformat()


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
    state: StateStore, events: EventLog, run_id: str, in_flight_node_id: str | None, error: str
) -> None:
    """Best-effort finalization of a crashed Run; never masks the original exception.

    Suppresses BaseException, not just Exception: a second KeyboardInterrupt or
    SystemExit arriving mid-finalization must not replace the crash being reported.
    """
    if in_flight_node_id is not None:
        with contextlib.suppress(BaseException):
            state.record_node_finished(run_id=run_id, node_id=in_flight_node_id, status="errored")
    with contextlib.suppress(BaseException):
        state.record_run_errored(run_id=run_id, error=error, finished_at=_now())
    with contextlib.suppress(BaseException):
        events.append("run_errored", {"error": error, "node_id": in_flight_node_id})


async def execute_run(workflow: Workflow, runs_root: Path) -> RunResult:
    """Materialize a run directory, execute the Workflow's Nodes, and persist the Run."""
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
        node_results: list[NodeResult] = []
        in_flight_node_id: str | None = None
        try:
            for node in execution_order(workflow):
                in_flight_node_id = node.id
                state.record_node_started(run_id=run_id, node_id=node.id)
                events.append("node_started", {"node_id": node.id, "attempt": 1})
                node_result = await _execute_shell_node(node)
                state.record_attempt(
                    run_id=run_id,
                    node_id=node.id,
                    attempt=1,
                    started_at=node_result.started_at,
                    finished_at=node_result.finished_at,
                    exit_status=node_result.exit_status,
                    output=node_result.normalized_output,
                )
                state.record_node_finished(
                    run_id=run_id, node_id=node.id, status=node_result.status
                )
                events.append(
                    "node_finished",
                    {
                        "node_id": node.id,
                        "attempt": 1,
                        "exit_status": node_result.exit_status,
                        "status": node_result.status,
                    },
                )
                in_flight_node_id = None
                node_results.append(node_result)
                if not node_result.succeeded:
                    # Pipeline semantics: a node failure stops the run; later nodes
                    # are never attempted (issue #26).
                    break
            run_result = RunResult(run_id=run_id, node_results=tuple(node_results))
            state.record_run_finished(run_id=run_id, status=run_result.status, finished_at=_now())
            events.append("run_finished", {"status": run_result.status})
        except BaseException as exc:
            message = str(exc)
            error = f"{type(exc).__name__}: {message}" if message else type(exc).__name__
            _finalize_crashed_run(state, events, run_id, in_flight_node_id, error)
            raise
    return run_result
