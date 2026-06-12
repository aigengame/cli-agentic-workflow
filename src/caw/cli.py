"""The caw command-line interface.

Exit code contract:

- 0: success (`caw run`: the Run succeeded; `caw validate`: the workflow
  is valid)
- 1: the Run finished with a failed Node (`caw run` only)
- 2: config error (unreadable file or invalid workflow definition);
  config errors print exactly one `error:` line
- 3: infrastructure error (e.g. unwritable runs root, State database
  failure) — the Run could not be executed or completed (`caw run` only)

Carve-out: command-line usage errors (unknown options, invalid option
values) also exit 2, but render the framework's multi-line usage message
without an `error:` prefix. Only workflow config errors are guaranteed
the single `error:` line.

`caw validate` never executes anything: no run directory is created and
no subprocess is spawned.
"""

import asyncio
import json
import sqlite3
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

import typer

from caw.config import WorkflowConfigError, load_workflow_file
from caw.executor import NodeResult, execute_run
from caw.model import Workflow, execution_order, normalize_workflow

app = typer.Typer(
    name="caw",
    help="caw: run explicit, inspectable, repeatable workflows over agent CLIs.",
    no_args_is_help=True,
)

_STDERR_EXCERPT_LINES = 20


def _echo_stderr_excerpt(node_result: NodeResult) -> None:
    lines = node_result.stderr.splitlines()
    excerpt = lines[-_STDERR_EXCERPT_LINES:]
    label = f"last {len(excerpt)} of {len(lines)} lines" if len(lines) > len(excerpt) else "stderr"
    typer.echo(f"node {node_result.node_id} stderr ({label}):", err=True)
    for line in excerpt:
        typer.echo(f"  {line}", err=True)


@app.callback()
def main() -> None:
    """caw: run explicit, inspectable, repeatable workflows over agent CLIs."""


def _load_normalized_workflow(workflow_file: Path) -> Workflow:
    """Load and normalize a workflow file, or exit 2 with one `error:` line."""
    try:
        raw = load_workflow_file(workflow_file)
        return normalize_workflow(raw, source=str(workflow_file))
    except WorkflowConfigError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc


@app.command()
def validate(workflow_file: Path) -> None:
    """Validate a workflow file without executing anything."""
    workflow = _load_normalized_workflow(workflow_file)
    typer.echo(f"workflow {workflow_file} is valid ({len(workflow.nodes)} nodes)")


class GraphFormat(StrEnum):
    """Output formats of `caw graph`."""

    text = "text"
    json = "json"


def _json_plan(workflow: Workflow) -> dict[str, Any]:
    """The machine-readable plan: nodes in declaration order, edges, execution order."""
    return {
        "workflow": workflow.name,
        "nodes": [
            {"id": node.id, "kind": node.kind, "needs": list(node.needs)}
            for node in workflow.nodes
        ],
        "edges": [
            {"from": dependency, "to": node.id}
            for node in workflow.nodes
            for dependency in node.needs
        ],
        "order": [node.id for node in execution_order(workflow)],
    }


@app.command()
def graph(
    workflow_file: Path,
    format: Annotated[
        GraphFormat, typer.Option(help="Render the plan as human-readable text or as JSON.")
    ] = GraphFormat.text,
) -> None:
    """Render the planned execution graph of a workflow file without executing it."""
    workflow = _load_normalized_workflow(workflow_file)
    if format is GraphFormat.json:
        typer.echo(json.dumps(_json_plan(workflow), indent=2))
        return
    typer.echo(f"workflow {workflow.name}: {len(workflow.nodes)} nodes")
    for position, node in enumerate(execution_order(workflow), start=1):
        needs = f"  (needs: {', '.join(node.needs)})" if node.needs else ""
        typer.echo(f"  {position}. {node.id}{needs}")


@app.command()
def run(workflow_file: Path) -> None:
    """Run a workflow file and print a plain-text result."""
    workflow = _load_normalized_workflow(workflow_file)
    runs_root = Path.cwd() / ".caw" / "runs"
    try:
        result = asyncio.run(execute_run(workflow, runs_root))
    except (OSError, sqlite3.Error) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=3) from exc
    for node_result in result.node_results:
        typer.echo(f"node {node_result.node_id} attempt 1 exited {node_result.exit_status}")
        if not node_result.succeeded and node_result.stderr:
            _echo_stderr_excerpt(node_result)
    if not result.succeeded:
        typer.echo(f"run {result.run_id} failed")
        raise typer.Exit(code=1)
    typer.echo(f"run {result.run_id} succeeded")
