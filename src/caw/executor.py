"""Execute one Run of a normalized Workflow on the local Engine Backend (ADR 0003)."""

import asyncio
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from caw.model import Node, Workflow, definition_checksum, workflow_snapshot
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
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    exit_status = process.returncode if process.returncode is not None else -1
    return NodeResult(
        node_id=node.id,
        exit_status=exit_status,
        stdout=stdout.decode(errors="replace"),
        stderr=stderr.decode(errors="replace"),
        started_at=started_at,
        finished_at=_now(),
    )


async def execute_run(workflow: Workflow, runs_root: Path) -> RunResult:
    """Materialize a run directory, execute the Workflow's Nodes, and persist the Run."""
    run_id = _new_run_id()
    run_dir = runs_root / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "workflow.normalized.json").write_text(
        json.dumps(workflow_snapshot(workflow), indent=2) + "\n", encoding="utf-8"
    )
    (run_dir / "events.jsonl").touch()

    with StateStore(run_dir / "state.sqlite") as state:
        state.record_run_started(
            run_id=run_id,
            workflow_name=workflow.name,
            definition_checksum=definition_checksum(workflow),
            created_at=_now(),
        )
        node_results: list[NodeResult] = []
        for node in workflow.nodes:
            state.record_node_started(run_id=run_id, node_id=node.id)
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
            node_results.append(node_result)
        run_result = RunResult(run_id=run_id, node_results=tuple(node_results))
        state.record_run_finished(
            run_id=run_id, status=run_result.status, finished_at=_now()
        )
    return run_result
