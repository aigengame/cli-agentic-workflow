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
from caw.controller import (
    ControllerError,
    GroupResult,
    load_controller_spec,
    resume_loop_until_done,
    run_loop_until_done,
)
from caw.executor import (
    SKIP_ALL_BRANCHES_SKIPPED,
    SKIP_BLOCKED,
    SKIP_WHEN_FALSE,
    NodeResult,
    ResumeError,
    RunResult,
    execute_run,
    resume_run,
)
from caw.model import Node, Predicate, Workflow, execution_order, normalize_workflow
from caw.patterns import expander_names, get_expander
from caw.report import GroupReportError, ReportFormat, render_group_report, render_report
from caw.runlayout import run_dir, runs_root
from caw.scaffold import PATTERN_EXAMPLES, STARTER_WORKFLOW

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


def _write_scaffold(target: Path, content: str, label: str) -> None:
    """Write a scaffolded workflow file, refusing to clobber an existing one.

    Scaffolding never silently overwrites an author's file: an existing target is a
    config-class refusal (exit 2, one ``error:`` line), mirroring the CLI's config
    contract. ``label`` names what was scaffolded (a starter, or a pattern example).
    """
    if target.exists():
        typer.echo(f"error: {target} already exists; refusing to overwrite it", err=True)
        raise typer.Exit(code=2)
    try:
        target.write_text(content, encoding="utf-8")
    except OSError as exc:
        typer.echo(f"error: cannot write {target}: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(f"created {label} at {target} (validate it with `caw validate {target}`)")


@app.command()
def init(
    path: Annotated[Path, typer.Argument(help="Where to write the starter workflow.")] = Path(
        "workflow.yaml"
    ),
) -> None:
    """Create a minimal starter workflow that validates and runs."""
    _write_scaffold(path, STARTER_WORKFLOW, "starter workflow")


patterns_app = typer.Typer(
    name="patterns",
    help="List and scaffold built-in workflow patterns.",
    no_args_is_help=True,
)
app.add_typer(patterns_app)


@patterns_app.command("list")
def patterns_list() -> None:
    """List the built-in workflow patterns and their one-line shapes.

    Driven off the expander registry, so every registered pattern appears and a
    newly registered expander shows up with no edit here.
    """
    for name in expander_names():
        expander = get_expander(name)
        assert expander is not None  # name came from the registry
        typer.echo(f"{name}: {expander.shape}")


@patterns_app.command("init")
def patterns_init(
    name: Annotated[str, typer.Argument(help="The built-in pattern to scaffold.")],
    path: Annotated[
        Path | None,
        typer.Argument(help="Where to write the workflow (defaults to <name>.yaml)."),
    ] = None,
) -> None:
    """Scaffold a complete, runnable example of a built-in pattern.

    Writes the workflow file plus any companion fixture files beside it, so the
    scaffolded bundle runs to success offline with the mock Adapter. The chosen
    path names the workflow file; companions are written in its directory. The
    whole bundle is written only if NO target file exists and no two bundle files
    collide on one destination, so an existing file is never clobbered and the
    workflow is never overwritten by one of its own companions.
    """
    example = PATTERN_EXAMPLES.get(name)
    if example is None:
        known = ", ".join(sorted(PATTERN_EXAMPLES)) or "<none>"
        typer.echo(f"error: unknown pattern {name!r} (known: {known})", err=True)
        raise typer.Exit(code=2)
    workflow_path = path if path is not None else Path(example.workflow_filename)
    directory = workflow_path.parent
    # Map every bundle file to its destination: the workflow under the chosen path,
    # each companion fixture beside it in the same directory.
    targets = {
        filename: (
            workflow_path if filename == example.workflow_filename else directory / filename
        )
        for filename in example.files
    }
    # Guard the WHOLE bundle before writing a single file. First: a chosen workflow
    # path whose name collides with a companion fixture maps two bundle files to ONE
    # destination — the write loop would overwrite the workflow with fixture JSON yet
    # still report success, leaving a non-runnable "workflow". Reject the collision.
    by_destination: dict[Path, str] = {}
    for filename, target in targets.items():
        resolved = target.resolve()
        collides_with = by_destination.get(resolved)
        if collides_with is not None:
            typer.echo(
                f"error: {target} is the destination of both {collides_with!r} and "
                f"{filename!r} in the {name} bundle; choose a workflow path whose name "
                f"does not collide with a companion file",
                err=True,
            )
            raise typer.Exit(code=2)
        by_destination[resolved] = filename
    # Second: refuse the whole bundle if any target exists, so a partial scaffold
    # never clobbers one file and leaves the rest unwritten.
    for target in targets.values():
        if target.exists():
            typer.echo(f"error: {target} already exists; refusing to overwrite it", err=True)
            raise typer.Exit(code=2)
    for filename, target in targets.items():
        try:
            target.write_text(example.files[filename], encoding="utf-8")
        except OSError as exc:
            typer.echo(f"error: cannot write {target}: {exc}", err=True)
            raise typer.Exit(code=2) from exc
    typer.echo(
        f"created {name} pattern example at {workflow_path} "
        f"(run it with `caw run {workflow_path}`)"
    )


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
            {
                "id": node.id,
                "kind": node.kind,
                "needs": list(node.needs),
                # The conditional structure (#7): a Node's `when` predicate and its
                # `join` policy, so a consumer reads the gating without running it.
                # `when` is the serialized predicate algebra (None when
                # unconditional). `to_plan_dict` emits exactly the active shape's
                # keys (#77) — a leaf's meaningful `value` (including a falsy 0 /
                # false / "") survives and a leaf's `path` appears only when it
                # addresses a structured_output sub-path — without the
                # `exclude_none` hazard of stripping an intentional key as if it
                # were an inactive-shape None. `join` is always shown (defaults to
                # all).
                "when": (node.when.to_plan_dict() if node.when is not None else None),
                "join": node.join,
            }
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
        typer.echo(f"  {position}. {node.id}{_node_annotations(node)}")


def _node_annotations(node: Node) -> str:
    """The text-plan annotations after a Node's id: needs, `when`, non-default join (#7).

    Additive and noise-free: an unconditional default-join Node shows only its
    needs (or nothing), so the `when` and `join` annotations appear solely where
    they carry signal — a conditional or skip-tolerant Node.
    """
    parts: list[str] = []
    if node.needs:
        parts.append(f"needs: {', '.join(node.needs)}")
    if node.when is not None:
        parts.append(f"when: {_predicate_summary(node.when)}")
    if node.join != "all":
        parts.append(f"join: {node.join}")
    return f"  ({'; '.join(parts)})" if parts else ""


def _predicate_summary(predicate: Predicate) -> str:
    """A compact one-line rendering of a `when` predicate's structure (#7).

    Mirrors the typed algebra without re-implementing evaluation: a leaf reads
    ``node.field[.path] op value``; a combinator names its shape and renders its
    children. The shape recursion is a ``Predicate.fold`` (#77), so this consumer
    drives the single shape-dispatch site rather than re-deriving it. The JSON plan
    carries the full serialized predicate, so this stays a human glance, not the
    authoritative form.
    """

    def _leaf(node: Predicate) -> str:
        assert node.ref is not None, "the fold's leaf callback receives a leaf"
        # A structured_output sub-path is shown as a dotted suffix so a routing
        # gate reads as `classify.structured_output.category equals 'bug'`.
        path = "".join(f".{step}" for step in node.ref.path)
        return f"{node.ref.node}.{node.ref.field}{path} {node.op} {node.value!r}"

    return predicate.fold(
        leaf=_leaf,
        all_of=lambda children: f"all_of({', '.join(children)})",
        any_of=lambda children: f"any_of({', '.join(children)})",
        not_=lambda child: f"not({child})",
    )


def _failure_line(workflow_label: str, node_result: NodeResult) -> str:
    """Name the workflow file, node id, and adapter of a failed Node (#6.5).

    A failure's surfaced message must locate it across the definition, the graph,
    and the integration, so a user need not cross-reference to find the source.
    The Adapter is named only for an agent Node (a shell Node has none); the
    classification (failed / timed_out / errored) tells WHY it failed.
    """
    adapter = f" (adapter {node_result.adapter})" if node_result.adapter else ""
    return f"workflow {workflow_label}: node {node_result.node_id}{adapter} {node_result.status}"


def _skip_reason(result: RunResult, node_id: str) -> str:
    """Render WHY a Node was skipped, distinct per cause (#7).

    AC5: a closed `when` gate, a failure-blocked dependent, and a fully-skipped
    tolerant join must each read distinctly — and all three distinctly from
    success and failure. The cause comes from State via ``RunResult``; a
    ``blocked`` skip also names the blocker that withheld it.
    """
    cause = result.skipped_causes.get(node_id)
    if cause == SKIP_WHEN_FALSE:
        return "(when false)"
    if cause == SKIP_ALL_BRANCHES_SKIPPED:
        return "(all branches skipped)"
    if cause == SKIP_BLOCKED:
        blocker = result.skipped_blockers.get(node_id)
        return f"(blocked by {blocker})" if blocker else "(blocked)"
    # A skip with no recorded cause is unexpected, but never crash the summary.
    blocker = result.skipped_blockers.get(node_id)
    return f"(blocked by {blocker})" if blocker else ""


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
        typer.echo(
            f"node {node_result.node_id} attempt {node_result.attempt} "
            f"exited {node_result.exit_status}"
        )
        if not node_result.succeeded:
            typer.echo(_failure_line(workflow_label, node_result))
            if node_result.stderr:
                _echo_stderr_excerpt(node_result)
    for node_id in result.skipped_node_ids:
        typer.echo(f"node {node_id} skipped {_skip_reason(result, node_id)}")
    if not result.succeeded:
        typer.echo(f"run {result.run_id} failed")
        raise typer.Exit(code=1)
    typer.echo(f"run {result.run_id} succeeded")


@app.command()
def run(workflow_file: Path) -> None:
    """Run a workflow file and print a plain-text result."""
    workflow = _load_normalized_workflow(workflow_file)
    try:
        result = asyncio.run(execute_run(workflow, runs_root()))
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
    try:
        result = asyncio.run(resume_run(run_id, runs_root()))
    except ResumeError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    except (OSError, sqlite3.Error) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=3) from exc
    _report_and_exit(result, workflow_label=run_id)


@app.command()
def report(
    run_id: str,
    format: Annotated[
        ReportFormat, typer.Option(help="Render the report as JSON.")
    ] = ReportFormat.json,
) -> None:
    """Render a report of a persisted run from its State and Events, without re-running it.

    An unknown run id, or a run directory whose State is missing, is refused as a
    config-class error (exit 2) with a single ``error:`` line, mirroring ``resume``; a
    report renders only from persisted data and never executes or mutates anything.
    """
    directory = run_dir(run_id)
    if not directory.is_dir():
        typer.echo(f"error: no run directory for run id {run_id!r} under {runs_root()}", err=True)
        raise typer.Exit(code=2)
    if not (directory / "state.sqlite").is_file():
        typer.echo(
            f"error: run directory for run id {run_id!r} has no state.sqlite "
            f"(incomplete or corrupt run); nothing to report",
            err=True,
        )
        raise typer.Exit(code=2)
    typer.echo(render_report(directory, format))


loop_app = typer.Typer(
    name="loop",
    help="Run, resume, and report a loop-until-done Run Group (a Pattern Controller).",
    no_args_is_help=True,
)
app.add_typer(loop_app)


def _report_group_and_exit(result: GroupResult) -> None:
    """Print a Run Group's outcome and exit on its stop reason (ADR 0009).

    Mirrors the single-run exit contract: the loop reaching ``done`` or stopping at
    ``exhausted`` (max iterations) is a successful drive (exit 0); a constituent Run
    that ``failed`` exits 1, matching ``caw run``'s failed-Run contract. Each
    iteration's run id is named so a user can drill into one with ``caw report``.
    """
    typer.echo(
        f"run group {result.group_id}: {result.status} ({len(result.iterations)} iterations)"
    )
    for iteration in result.iterations:
        outcome = "succeeded" if iteration.succeeded else "failed"
        typer.echo(f"  iteration {iteration.iteration_index} ({iteration.run_id}): {outcome}")
    if result.status == "failed":
        raise typer.Exit(code=1)


@loop_app.command("run")
def loop_run(spec_file: Path) -> None:
    """Run a loop-until-done Run Group from a controller spec file.

    Materializes each iteration as a separate immutable Run, feeding the prior
    iteration's output forward, until the done-predicate holds, an iteration fails,
    or ``max_iterations`` is reached. Exit codes mirror ``caw run``: 0 (group done
    or exhausted), 1 (a constituent Run failed), 2 (an invalid spec), 3 (infra).
    """
    try:
        spec = load_controller_spec(spec_file)
    except WorkflowConfigError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    try:
        result = asyncio.run(run_loop_until_done(spec, base=Path.cwd()))
    except ControllerError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    except WorkflowConfigError as exc:
        # An iteration workflow that fails to normalize is a config-class refusal.
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    except (OSError, sqlite3.Error) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=3) from exc
    _report_group_and_exit(result)


@loop_app.command("resume")
def loop_resume(group_id: str) -> None:
    """Resume an interrupted Run Group, continuing without re-running completed iterations.

    The Run Group is the resumption unit (ADR 0002): a succeeded iteration is never
    re-run. An unknown or already-finished group is refused as a config-class error
    (exit 2). Otherwise the exit contract mirrors ``caw loop run``.
    """
    try:
        result = asyncio.run(resume_loop_until_done(group_id, base=Path.cwd()))
    except ControllerError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    except WorkflowConfigError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    except (OSError, sqlite3.Error) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=3) from exc
    _report_group_and_exit(result)


@loop_app.command("report")
def loop_report(
    group_id: str,
    format: Annotated[
        ReportFormat,
        typer.Option(help="Render the aggregate report as JSON, JSONL, text, or markdown."),
    ] = ReportFormat.json,
) -> None:
    """Render an aggregate report of a Run Group from persisted State and Events.

    Aggregates every iteration into one result (AC6), never re-executing. An unknown
    group id is refused as a config-class error (exit 2) with one ``error:`` line.
    """
    try:
        rendered = render_group_report(group_id, Path.cwd(), format)
    except GroupReportError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(rendered)
