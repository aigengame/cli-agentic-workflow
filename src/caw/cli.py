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
import sys
from collections.abc import Coroutine
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

import typer

from caw import __version__
from caw.config import WorkflowConfigError, load_workflow_file
from caw.controller import (
    AdversarialSpec,
    ControllerError,
    GroupResult,
    TournamentSpec,
    load_controller_spec,
    load_spec_file,
    resume_adversarial_verification,
    resume_loop_until_done,
    resume_tournament,
    run_adversarial_verification,
    run_loop_until_done,
    run_tournament,
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
from caw.model import (
    HumanGateNodeInputs,
    Node,
    Predicate,
    Workflow,
    execution_order,
    normalize_workflow,
)
from caw.patterns import expander_names, get_expander
from caw.report import GroupReportError, ReportFormat, render_group_report, render_report
from caw.runlayout import run_dir, runs_root
from caw.scaffold import (
    ADVERSARIAL_EXAMPLE,
    LOOP_EXAMPLE,
    PATTERN_EXAMPLES,
    STARTER_WORKFLOW,
    TOURNAMENT_EXAMPLE,
    PatternExample,
)
from caw.status import FAILED, SUCCEEDED

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


def _version_callback(value: bool) -> None:
    """Print the caw version and exit before any subcommand runs.

    Eager so `caw --version` short-circuits parsing: it reads `caw.__version__`
    (the release-please-maintained version literal, ADR 0005 -- it does not compute
    a version) and exits 0 without requiring a subcommand (#113).
    """
    if value:
        typer.echo(f"caw {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            "-V",
            help="Show the caw version and exit.",
            is_eager=True,
            callback=_version_callback,
        ),
    ] = False,
) -> None:
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
    workflow_path = _write_example_bundle(example, path, label=f"the {name} bundle")
    typer.echo(
        f"created {name} pattern example at {workflow_path} "
        f"(run it with `caw run {workflow_path}`)"
    )


def _write_example_bundle(example: PatternExample, path: Path | None, *, label: str) -> Path:
    """Write a complete scaffold bundle all-or-nothing, returning the primary file path.

    Shared by ``caw patterns init`` and ``caw loop init`` (#15): the primary file is
    written under the chosen ``path`` (default: the example's filename) and each
    companion beside it in the same directory. The whole bundle is guarded before a
    single file is written, so an existing file is never clobbered and a chosen path
    that collides with a companion is refused — never leaving a partial scaffold.
    ``label`` names the bundle in the collision message.
    """
    workflow_path = path if path is not None else Path(example.workflow_filename)
    directory = workflow_path.parent
    # Map every bundle file to its destination: the primary under the chosen path,
    # each companion beside it in the same directory.
    targets = {
        filename: (
            workflow_path if filename == example.workflow_filename else directory / filename
        )
        for filename in example.files
    }
    # Guard the WHOLE bundle before writing a single file. First: a chosen path whose
    # name collides with a companion maps two bundle files to ONE destination — the
    # write loop would overwrite the primary with companion content yet still report
    # success, leaving a non-runnable bundle. Reject the collision.
    by_destination: dict[Path, str] = {}
    for filename, target in targets.items():
        resolved = target.resolve()
        collides_with = by_destination.get(resolved)
        if collides_with is not None:
            typer.echo(
                f"error: {target} is the destination of both {collides_with!r} and "
                f"{filename!r} in {label}; choose a path whose name "
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
    return workflow_path


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
    if result.rejected:
        # A human declined a gate, ending the run as `rejected` — a decided "no",
        # distinct from a failure (#10, ADR 0010). Exit 1: not a successful terminal.
        for node_id in result.rejected_node_ids:
            typer.echo(f"node {node_id} rejected")
        typer.echo(f"run {result.run_id} rejected")
        raise typer.Exit(code=1)
    if result.parked:
        # A parked Run is neither succeeded nor failed: it awaits approval at one or
        # more human gates (#10, ADR 0010). Name the awaiting gates and exit 0 — a
        # park is not a failure; the run is advanced later via `caw resume`.
        for node_id in result.awaiting_node_ids:
            typer.echo(f"node {node_id} awaiting approval")
        typer.echo(f"run {result.run_id} parked at a human gate")
        return
    for node_id in result.skipped_node_ids:
        typer.echo(f"node {node_id} skipped {_skip_reason(result, node_id)}")
    if not result.succeeded:
        typer.echo(f"run {result.run_id} failed")
        raise typer.Exit(code=1)
    typer.echo(f"run {result.run_id} succeeded")


def _is_attended() -> bool:
    """Whether this is an interactive (TTY) session that can prompt at a human gate (#10)."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def _drive_tty_gates(result: RunResult, gate_prompts: dict[str, str | None]) -> RunResult:
    """In an attended session, prompt at each awaiting gate and advance the run (#10).

    A parked run in a TTY prompts inline for every awaiting gate — yes approves it, no
    rejects it (and any rejection ends the run, ADR 0010) — then resumes with the
    decisions, looping until the run reaches a terminal (or a rejection ends it). In a
    non-TTY session the run stays parked for `caw resume`, so this is a no-op.
    """
    while result.parked and _is_attended():
        approvals: list[str] = []
        rejections: list[str] = []
        for node_id in result.awaiting_node_ids:
            prompt = gate_prompts.get(node_id) or f"Approve gate {node_id!r}?"
            (approvals if typer.confirm(prompt) else rejections).append(node_id)
        result = asyncio.run(
            resume_run(
                result.run_id,
                runs_root(),
                approvals=tuple(approvals),
                rejections=tuple(rejections),
            )
        )
    return result


@app.command()
def run(workflow_file: Path) -> None:
    """Run a workflow file and print a plain-text result.

    In an attended (TTY) session a human_gate prompts inline (#10): yes approves it and
    the run continues, no rejects it and ends the run. In a non-TTY session the run
    parks for `caw resume`.
    """
    workflow = _load_normalized_workflow(workflow_file)
    gate_prompts: dict[str, str | None] = {
        node.id: node.inputs.prompt
        for node in workflow.nodes
        if isinstance(node.inputs, HumanGateNodeInputs)
    }
    try:
        result = asyncio.run(execute_run(workflow, runs_root()))
        result = _drive_tty_gates(result, gate_prompts)
    except (OSError, sqlite3.Error) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=3) from exc
    _report_and_exit(result, workflow_label=str(workflow_file))


@app.command()
def resume(
    run_id: str,
    approve: Annotated[
        list[str] | None,
        typer.Option(
            "--approve",
            help="Approve an awaiting human gate by node id (repeatable).",
        ),
    ] = None,
    reject: Annotated[
        list[str] | None,
        typer.Option(
            "--reject",
            help="Reject an awaiting human gate by node id, ending the run (repeatable).",
        ),
    ] = None,
) -> None:
    """Resume a run: re-run an interrupted/failed run's incomplete nodes, or advance a
    parked run by approving or declining its awaiting human gates.

    Mirrors ``run``'s output and exit-code contract: 0 on success (or a clean park),
    1 on a failed node or a rejected run, 3 on an infrastructure error. An unknown
    run id or a run that already succeeded or was rejected is not resumable and is
    refused as a config-class error (exit 2) with a single ``error:`` line, never
    re-executing it.

    ``--approve <node-id>`` advances a parked run by approving an awaiting human
    gate (#10); a gate left unnamed re-parks. ``--reject <node-id>`` ends the run
    as rejected (exit 1); any rejection ends it, so a co-named approval does not
    save it. Approving or rejecting a node that is not an awaiting gate is the same
    config-class refusal (exit 2).
    """
    try:
        result = asyncio.run(
            resume_run(
                run_id,
                runs_root(),
                approvals=tuple(approve or ()),
                rejections=tuple(reject or ()),
            )
        )
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

    Shared by every Pattern Controller surface (``caw loop`` / ``caw verify`` /
    ``caw tournament``). Mirrors the single-run exit contract: any non-``failed``
    terminal status (``done`` / ``exhausted`` / ``accepted`` / ``rejected`` /
    ``complete``) is a successful drive (exit 0); a constituent Run that ``failed``
    exits 1, matching ``caw run``'s failed-Run contract. Each iteration's run id is
    named for provenance; a Run Group is the reporting unit (ADR 0009), so a user
    inspects the whole group with ``caw <controller> report <group_id>`` (the iteration
    runs live under ``groups/<id>/iterations/``, not the ``.caw/runs/`` root ``caw
    report`` resolves). The tournament's final winner is named when present.
    """
    typer.echo(
        f"Run Group {result.group_id}: {result.status} ({len(result.iterations)} iterations)"
    )
    for iteration in result.iterations:
        outcome = SUCCEEDED if iteration.succeeded else FAILED
        typer.echo(f"  iteration {iteration.iteration_index} ({iteration.run_id}): {outcome}")
    if result.winner is not None:
        typer.echo(f"winner: {result.winner}")
    if result.status == "failed":
        raise typer.Exit(code=1)


@loop_app.command("run")
def loop_run(spec_file: Path) -> None:
    """Run a loop-until-done Run Group from a controller spec file.

    Materializes each iteration as a separate immutable Run, feeding the prior
    iteration's output forward, until the done Predicate holds, an iteration fails,
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
    _report_group(group_id, format)


def _report_group(group_id: str, format: ReportFormat) -> None:
    """Render a Run Group's aggregate report, shared by every Controller surface (#15, #17).

    A Run Group is reported with the SAME aggregate renderer regardless of which
    Controller produced it (loop / verify / tournament), since every Controller
    persists the same ``group.json`` + per-iteration run layout. An unknown group id
    is a config-class refusal (exit 2) with one ``error:`` line.
    """
    try:
        rendered = render_group_report(group_id, Path.cwd(), format)
    except GroupReportError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(rendered)


@loop_app.command("init")
def loop_init(
    path: Annotated[
        Path | None,
        typer.Argument(help="Where to write the controller spec (defaults to loop.yaml)."),
    ] = None,
) -> None:
    """Scaffold a complete, runnable loop-until-done example (spec + workflow + fixtures).

    Writes the controller spec plus its iteration workflow and fixture companions
    beside it, so the bundle drives a loop to done offline with the mock Adapter as
    written — run it with ``caw loop run loop.yaml``. The whole bundle is written
    all-or-nothing, never clobbering an existing file.
    """
    spec_path = _write_example_bundle(LOOP_EXAMPLE, path, label="the loop-until-done bundle")
    typer.echo(
        f"created loop-until-done example at {spec_path} (run it with `caw loop run {spec_path}`)"
    )


def _drive_group_and_exit(coro: "Coroutine[Any, Any, GroupResult]") -> None:
    """Run a Controller coroutine to a GroupResult, mapping failures to the exit contract.

    Shared by every Controller's ``run``/``resume`` (#15, #17): a ControllerError or a
    config-class WorkflowConfigError (a bad spec / an iteration workflow that fails to
    normalize / an unknown-or-finished group) is exit 2 with one ``error:`` line; an
    infrastructure failure is exit 3; otherwise the group's outcome is reported and the
    success/failure exit contract applied by :func:`_report_group_and_exit`.
    """
    try:
        result = asyncio.run(coro)
    except (ControllerError, WorkflowConfigError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    except (OSError, sqlite3.Error) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=3) from exc
    _report_group_and_exit(result)


verify_app = typer.Typer(
    name="verify",
    help="Run, resume, and report an adversarial-verification Run Group (a Pattern Controller).",
    no_args_is_help=True,
)
app.add_typer(verify_app)


@verify_app.command("run")
def verify_run(spec_file: Path) -> None:
    """Run an adversarial-verification Run Group from a controller spec file (#17).

    Materializes each verification round as a separate immutable Run, feeding the
    verifier's feedback forward, until the accept Predicate holds (``accepted``), a
    round fails, or ``max_rounds`` is reached (``rejected``). Exit codes mirror
    ``caw run``: 0 (accepted / rejected), 1 (a constituent Run failed), 2 (an invalid
    spec), 3 (infra).
    """
    try:
        spec = load_spec_file(spec_file, AdversarialSpec)
    except WorkflowConfigError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    _drive_group_and_exit(run_adversarial_verification(spec, base=Path.cwd()))


@verify_app.command("resume")
def verify_resume(group_id: str) -> None:
    """Resume an interrupted adversarial-verification Run Group (#17).

    The Run Group is the resumption unit (ADR 0002): a succeeded round is never
    re-run. An unknown or already-finished group is refused as a config-class error
    (exit 2). Otherwise the exit contract mirrors ``caw verify run``.
    """
    _drive_group_and_exit(resume_adversarial_verification(group_id, base=Path.cwd()))


@verify_app.command("report")
def verify_report(
    group_id: str,
    format: Annotated[
        ReportFormat,
        typer.Option(help="Render the aggregate report as JSON, JSONL, text, or markdown."),
    ] = ReportFormat.json,
) -> None:
    """Render an aggregate report of an adversarial-verification Run Group (#17).

    Aggregates every round into one result, never re-executing. An unknown group id is
    refused as a config-class error (exit 2) with one ``error:`` line.
    """
    _report_group(group_id, format)


@verify_app.command("init")
def verify_init(
    path: Annotated[
        Path | None,
        typer.Argument(help="Where to write the controller spec (defaults to verify.yaml)."),
    ] = None,
) -> None:
    """Scaffold a complete, runnable adversarial-verification example (#17).

    Writes the controller spec plus its round workflow and fixture companions beside
    it, so the bundle drives a verification to accepted offline with the mock Adapter —
    run it with ``caw verify run verify.yaml``. Written all-or-nothing.
    """
    spec_path = _write_example_bundle(
        ADVERSARIAL_EXAMPLE, path, label="the adversarial-verification bundle"
    )
    typer.echo(
        f"created adversarial-verification example at {spec_path} "
        f"(run it with `caw verify run {spec_path}`)"
    )


tournament_app = typer.Typer(
    name="tournament",
    help="Run, resume, and report a tournament Run Group (a Pattern Controller).",
    no_args_is_help=True,
)
app.add_typer(tournament_app)


@tournament_app.command("run")
def tournament_run(spec_file: Path) -> None:
    """Run a tournament Run Group from a controller spec file (#17).

    Materializes each round as a separate immutable Run, promoting the round's winner
    into the next round, until every round has run (``complete``) or a round fails
    (``failed``). The final winner is named. Exit codes mirror ``caw run``: 0
    (complete), 1 (a constituent Run failed), 2 (an invalid spec), 3 (infra).
    """
    try:
        spec = load_spec_file(spec_file, TournamentSpec)
    except WorkflowConfigError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    _drive_group_and_exit(run_tournament(spec, base=Path.cwd()))


@tournament_app.command("resume")
def tournament_resume(group_id: str) -> None:
    """Resume an interrupted tournament Run Group (#17).

    The Run Group is the resumption unit (ADR 0002): a succeeded round is never re-run.
    An unknown or already-finished group is refused as a config-class error (exit 2).
    Otherwise the exit contract mirrors ``caw tournament run``.
    """
    _drive_group_and_exit(resume_tournament(group_id, base=Path.cwd()))


@tournament_app.command("report")
def tournament_report(
    group_id: str,
    format: Annotated[
        ReportFormat,
        typer.Option(help="Render the aggregate report as JSON, JSONL, text, or markdown."),
    ] = ReportFormat.json,
) -> None:
    """Render an aggregate report of a tournament Run Group, with comparison evidence (#17).

    Aggregates every round into one result — each round's compare-node output (the
    winner and the comparison scores) is the comparison evidence — never re-executing.
    An unknown group id is refused as a config-class error (exit 2) with one
    ``error:`` line.
    """
    _report_group(group_id, format)


@tournament_app.command("init")
def tournament_init(
    path: Annotated[
        Path | None,
        typer.Argument(help="Where to write the controller spec (defaults to tournament.yaml)."),
    ] = None,
) -> None:
    """Scaffold a complete, runnable tournament example (#17).

    Writes the controller spec plus its round workflow and fixture companions beside
    it, so the bundle runs a tournament to completion offline with the mock Adapter —
    run it with ``caw tournament run tournament.yaml``. Written all-or-nothing.
    """
    spec_path = _write_example_bundle(TOURNAMENT_EXAMPLE, path, label="the tournament bundle")
    typer.echo(
        f"created tournament example at {spec_path} (run it with `caw tournament run {spec_path}`)"
    )
