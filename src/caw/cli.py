"""The caw command-line interface.

Exit code contract:

- 0: success (`caw run` / `caw resume`: the Run succeeded; `caw validate`:
  the workflow is valid; `caw graph`: the plan was rendered)
- 1: the Run finished with a failed Node (`caw run`, `caw resume`)
- 2: config error (unreadable file or invalid workflow definition);
  config errors print exactly one `error:` line. `caw resume` also exits 2
  when the run id is unknown or the Run is not resume-eligible (it already
  succeeded) — a refusal, with one `error:` line and no re-execution.
- 3: infrastructure error (e.g. unwritable runs root, State database
  failure) — the Run could not be executed or completed (`caw run`,
  `caw resume`)

Carve-out: command-line usage errors (unknown options, invalid option
values) also exit 2, but render the framework's multi-line usage message
without an `error:` prefix. Only workflow config errors are guaranteed
the single `error:` line.

`caw validate` and `caw graph` never execute anything: no run directory
is created and no subprocess is spawned. `caw resume` reopens an EXISTING
run directory and re-runs only its incomplete Nodes, reusing the same run
id, State, and Events trace.
"""

import asyncio
import json
import sqlite3
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

import typer

from caw.config import WorkflowConfigError, load_workflow_file
from caw.executor import NodeResult, ResumeError, RunResult, execute_run, resume_run
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
        # Anchor relative agent-Node paths to the workflow file's directory so the
        # same definition validates and runs identically from any cwd (#64).
        return normalize_workflow(
            raw, source=str(workflow_file), base_dir=workflow_file.resolve().parent
        )
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
    """The machine-readable plan: nodes in declaration order, edges, topological order.

    "topological_order" is a topological linearization of the dependency graph
    with declaration order breaking ties among ready nodes — not a promise that
    nodes execute strictly sequentially (parallel scheduling is issue #4).
    """
    return {
        "workflow": workflow.name,
        "concurrency": workflow.concurrency,
        "nodes": [
            {"id": node.id, "kind": node.kind, "needs": list(node.needs)}
            for node in workflow.nodes
        ],
        "edges": [
            {"from": dependency, "to": node.id}
            for node in workflow.nodes
            for dependency in node.needs
        ],
        "topological_order": [node.id for node in execution_order(workflow)],
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
    typer.echo(
        f"workflow {workflow.name}: {len(workflow.nodes)} nodes "
        f"(concurrency: {workflow.concurrency})"
    )
    for position, node in enumerate(execution_order(workflow), start=1):
        needs = f"  (needs: {', '.join(node.needs)})" if node.needs else ""
        typer.echo(f"  {position}. {node.id}{needs}")


def _failure_line(workflow_label: str, node_result: NodeResult) -> str:
    """Name the workflow file, node id, and adapter of a failed Node (#6.5).

    A failure's surfaced message must locate it across the definition, the graph,
    and the integration, so a user need not cross-reference to find the source.
    The Adapter is named only for an agent Node (a shell Node has none); the
    classification (failed / timed_out / errored) tells WHY it failed.
    """
    adapter = f" (adapter {node_result.adapter})" if node_result.adapter else ""
    return f"workflow {workflow_label}: node {node_result.node_id}{adapter} {node_result.status}"


def _report_and_exit(result: RunResult, workflow_label: str) -> None:
    """Print a Run's plain-text result and exit on the Run's success contract.

    Shared by ``run`` and ``resume`` so a resumed Run reports identically: each
    attempted Node's terminal status, a failed Node's locating message and stderr
    excerpt, the withheld skipped Nodes and their blockers, then the run-level
    success/failure line. ``workflow_label`` is the workflow file path for ``run``
    and the run id for ``resume``, so the failure message can name its source in
    both. A failed Run exits 1; a succeeded Run returns 0 by falling through.
    """
    for node_result in result.node_results:
        typer.echo(f"node {node_result.node_id} attempt 1 exited {node_result.exit_status}")
        if not node_result.succeeded:
            typer.echo(_failure_line(workflow_label, node_result))
            if node_result.stderr:
                _echo_stderr_excerpt(node_result)
    for node_id in result.skipped_node_ids:
        blocker = result.skipped_blockers.get(node_id)
        blocked_by = f" (blocked by {blocker})" if blocker else ""
        typer.echo(f"node {node_id} skipped{blocked_by}")
    if not result.succeeded:
        typer.echo(f"run {result.run_id} failed")
        raise typer.Exit(code=1)
    typer.echo(f"run {result.run_id} succeeded")


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
    _report_and_exit(result, workflow_label=str(workflow_file))


@app.command()
def resume(run_id: str) -> None:
    """Resume an interrupted or failed run, re-running only its incomplete nodes.

    Mirrors ``run``'s output and exit-code contract: 0 on success, 1 on a failed
    node, 3 on an infrastructure error. An unknown run id or a run that already
    succeeded is not resumable and is refused as a config-class error (exit 2)
    with a single ``error:`` line, never re-executing it.
    """
    runs_root = Path.cwd() / ".caw" / "runs"
    try:
        result = asyncio.run(resume_run(run_id, runs_root))
    except ResumeError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    except (OSError, sqlite3.Error) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=3) from exc
    _report_and_exit(result, workflow_label=run_id)
