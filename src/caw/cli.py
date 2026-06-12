"""The caw command-line interface.

Exit code contract of `caw run`:

- 0: the Run succeeded
- 1: the Run finished with a failed Node
- 2: config error (unreadable file or invalid workflow definition)
- 3: infrastructure error (e.g. unwritable runs root, State database
  failure) — the Run could not be executed or completed
"""

import asyncio
import sqlite3
from pathlib import Path

import typer

from caw.config import WorkflowConfigError, load_workflow_file
from caw.executor import NodeResult, execute_run
from caw.model import normalize_workflow

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


@app.command()
def run(workflow_file: Path) -> None:
    """Run a workflow file and print a plain-text result."""
    try:
        raw = load_workflow_file(workflow_file)
        workflow = normalize_workflow(raw, source=str(workflow_file))
    except WorkflowConfigError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
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
