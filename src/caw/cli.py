"""The caw command-line interface."""

import asyncio
from pathlib import Path

import typer

from caw.config import load_workflow_file
from caw.executor import execute_run
from caw.model import normalize_workflow

app = typer.Typer(
    name="caw",
    help="caw: run explicit, inspectable, repeatable workflows over agent CLIs.",
    no_args_is_help=True,
)


@app.callback()
def main() -> None:
    """caw: run explicit, inspectable, repeatable workflows over agent CLIs."""


@app.command()
def run(workflow_file: Path) -> None:
    """Run a workflow file and print a plain-text result."""
    raw = load_workflow_file(workflow_file)
    workflow = normalize_workflow(raw, source=str(workflow_file))
    runs_root = Path.cwd() / ".caw" / "runs"
    result = asyncio.run(execute_run(workflow, runs_root))
    for node_result in result.node_results:
        typer.echo(
            f"node {node_result.node_id} attempt 1 "
            f"exited {node_result.exit_status}"
        )
    if not result.succeeded:
        typer.echo(f"run {result.run_id} failed")
        raise typer.Exit(code=1)
    typer.echo(f"run {result.run_id} succeeded")
