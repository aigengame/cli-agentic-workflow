"""Execute one Run of a normalized Workflow on the local Engine Backend (ADR 0003)."""

import asyncio
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from caw.model import Node, Workflow, workflow_snapshot
from caw.state import initialize_state


@dataclass(frozen=True)
class NodeResult:
    """The normalized output of one Node Attempt."""

    node_id: str
    exit_status: int
    stdout: str
    stderr: str

    @property
    def succeeded(self) -> bool:
        return self.exit_status == 0


@dataclass(frozen=True)
class RunResult:
    """The outcome of one Run."""

    run_id: str
    node_results: tuple[NodeResult, ...]

    @property
    def succeeded(self) -> bool:
        return all(result.succeeded for result in self.node_results)


def _new_run_id() -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{secrets.token_hex(4)}"


async def _execute_shell_node(node: Node) -> NodeResult:
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
    )


async def execute_run(workflow: Workflow, runs_root: Path) -> RunResult:
    """Materialize a run directory, execute the Workflow's Nodes, and persist the Run."""
    run_id = _new_run_id()
    run_dir = runs_root / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "workflow.normalized.json").write_text(
        json.dumps(workflow_snapshot(workflow), indent=2) + "\n", encoding="utf-8"
    )
    initialize_state(run_dir / "state.sqlite")
    (run_dir / "events.jsonl").touch()

    node_results = [await _execute_shell_node(node) for node in workflow.nodes]
    return RunResult(run_id=run_id, node_results=tuple(node_results))
