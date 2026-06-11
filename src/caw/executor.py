"""Execute one Run of a normalized Workflow on the local Engine Backend (ADR 0003)."""

import asyncio
from dataclasses import dataclass

from caw.model import Node, Workflow


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

    node_results: tuple[NodeResult, ...]

    @property
    def succeeded(self) -> bool:
        return all(result.succeeded for result in self.node_results)


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


async def execute_run(workflow: Workflow) -> RunResult:
    """Execute the Workflow's Nodes in definition order and return the Run outcome."""
    node_results = [await _execute_shell_node(node) for node in workflow.nodes]
    return RunResult(node_results=tuple(node_results))
