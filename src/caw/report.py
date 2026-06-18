"""Render a Run's persisted State and Events as a report (#12).

A Reporter renders exclusively from what a Run left on disk — its State, its
normalized-workflow snapshot, and its Event trace — so reports work identically
for completed, failed, and parked Runs and never re-execute anything. Every
report keeps the Run's *conclusion* (the run, node statuses, artifacts, errors)
distinct from the *trace evidence* (the append-only events).
"""

import json
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Any

from caw.contract import OutputContractError, validate_output_contract
from caw.runlayout import group_iterations_root, group_state_path
from caw.state import StateStore
from caw.status import SKIPPED, SUCCEEDED


class ReportFormat(StrEnum):
    """Output formats of `caw report`."""

    json = "json"
    jsonl = "jsonl"
    text = "text"
    markdown = "markdown"


def _read_trace(run_dir: Path) -> list[dict[str, Any]]:
    """The Run's append-only Event sequence, parsed from ``events.jsonl``."""
    lines = (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _read_snapshot(run_dir: Path) -> dict[str, Any]:
    """The persisted normalized Workflow snapshot for this Run."""
    loaded: dict[str, Any] = json.loads(
        (run_dir / "workflow.normalized.json").read_text(encoding="utf-8")
    )
    return loaded


def _read_graph(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """The Run's nodes and dependency edges, read from the persisted snapshot.

    Reads the normalized snapshot rather than re-validating the Workflow, so the
    report reflects exactly the graph that ran, in declaration order.
    """
    return [
        {"id": node["id"], "needs": list(node.get("needs", []))}
        for node in snapshot["workflow"]["nodes"]
    ]


def _conclusion_nodes(run_dir: Path) -> tuple[str | None, list[dict[str, Any]]]:
    """The run status and each node's outcome, read from State.

    A node's ``exit_status`` / ``artifacts`` come from its latest Attempt's
    persisted output; a skipped or never-attempted node has none. ``cause`` carries a
    skipped node's reason (``when_false`` / ``blocked`` / ``all_branches_skipped``, #7)
    so the three skip reasons read distinctly. ``error`` carries a non-succeeded
    node's stderr so the report can surface why it failed. State is opened read-only —
    a report never mutates it (#12).
    """
    run_id = run_dir.name
    with StateStore(run_dir / "state.sqlite", read_only=True) as state:
        status = state.run_status(run_id)
        node_statuses = state.node_statuses(run_id)
        causes = state.node_causes(run_id)
        outputs = {node_id: state.node_output(run_id, node_id) for node_id in node_statuses}
    nodes = []
    for node_id in sorted(node_statuses):
        node_status = node_statuses[node_id]
        output = outputs[node_id] or {}
        stderr = output.get("stderr") or ""
        failed = node_status not in {SUCCEEDED, SKIPPED}
        nodes.append(
            {
                "id": node_id,
                "status": node_status,
                "exit_status": output.get("exit_status"),
                "cause": causes.get(node_id),
                "structured_output": output.get("structured_output"),
                "artifacts": list(output.get("artifacts", [])),
                "error": stderr if failed and stderr else None,
            }
        )
    return status, nodes


def _gather(run_dir: Path) -> dict[str, Any]:
    """Assemble the full report model from the Run's persisted State and Events."""
    snapshot = _read_snapshot(run_dir)
    status, nodes = _conclusion_nodes(run_dir)
    trace = _read_trace(run_dir)
    blockers = _skip_blockers(trace)
    for node in nodes:
        node["blocked_by"] = blockers.get(node["id"])
    return {
        "run_id": run_dir.name,
        "status": status,
        "nodes": nodes,
        "graph": _read_graph(snapshot),
        "trace": trace,
        "final_output": _validate_final_output(run_dir, snapshot),
    }


def _validate_final_output(run_dir: Path, snapshot: dict[str, Any]) -> dict[str, Any] | None:
    """Validate the declared final output from persisted State, if the Workflow has one."""
    declaration = snapshot["workflow"].get("final_output")
    if declaration is None:
        return None
    node_id = declaration["node"]
    field = declaration["field"]
    schema = declaration["schema"]
    with StateStore(run_dir / "state.sqlite", read_only=True) as state:
        output = state.node_output(run_dir.name, node_id) or {}
    value = output.get(field)
    try:
        validate_output_contract(Path(schema), value)
    except OutputContractError as exc:
        return {
            "node": node_id,
            "field": field,
            "schema": schema,
            "status": "invalid",
            "value": value,
            "error": str(exc),
        }
    return {
        "node": node_id,
        "field": field,
        "schema": schema,
        "status": "valid",
        "value": value,
        "error": None,
    }


def _skip_blockers(trace: list[dict[str, Any]]) -> dict[str, str | None]:
    """Map each skipped Node to the upstream that withheld it, from the Event trace.

    The executor records the ACTUAL blocker at skip-propagation time in each
    ``node_skipped`` event's ``blocked_by`` (#94) — the same authoritative source the
    live ``caw run`` message reads — so a report reads it back rather than re-deriving
    a blocker from final statuses, which would be ambiguous when a Node has several
    failed/skipped upstreams. A ``when_false`` / ``all_branches_skipped`` skip carries
    no ``blocked_by`` (no blocker); a resume that re-skips a Node appends a fresh
    event, so the LAST ``node_skipped`` per Node is authoritative. A ``blocked`` Node
    whose trace lacks the blocker degrades to ``None`` (a plain ``(blocked)``).
    """
    blockers: dict[str, str | None] = {}
    for event in trace:
        if event.get("type") != "node_skipped":
            continue
        data = event.get("data", {})
        node_id = data.get("node_id")
        if node_id is not None:
            blockers[node_id] = data.get("blocked_by")
    return blockers


def _render_json(report: dict[str, Any]) -> str:
    """Machine-readable conclusion + trace (the confirmed top-level contract, #12)."""
    contract = {key: report[key] for key in ("run_id", "status", "nodes", "trace")}
    if report["final_output"] is not None:
        contract["final_output"] = report["final_output"]
    return json.dumps(contract, indent=2)


def _render_jsonl(report: dict[str, Any]) -> str:
    """Line-delimited stream: a tagged conclusion record, then one record per event."""
    summary = {key: report[key] for key in ("run_id", "status", "nodes")}
    if report["final_output"] is not None:
        summary["final_output"] = report["final_output"]
    lines = [json.dumps({"record": "conclusion", **summary})]
    lines += [json.dumps({"record": "event", **event}) for event in report["trace"]]
    return "\n".join(lines)


def _render_text(report: dict[str, Any]) -> str:
    """Plain text: the conclusion first, then the trace under a distinct heading."""
    lines = [f"run {report['run_id']}: {report['status']}", "nodes:"]
    for node in report["nodes"]:
        lines.append(f"  {node['id']}: {node['status']}{_node_detail(node)}")
    if report["final_output"] is not None:
        final = report["final_output"]
        lines.append("final output:")
        lines.append(
            f"  {final['node']}.{final['field']}: {final['status']} (schema: {final['schema']})"
        )
        if final["error"]:
            lines.append(f"  error: {final['error']}")
    lines.append("trace:")
    for event in report["trace"]:
        lines.append(f"  {event['seq']:>4} {event['type']}")
    return "\n".join(lines)


def _render_markdown(report: dict[str, Any]) -> str:
    """Markdown: graph, node statuses, artifacts, and errors, then the trace (#12)."""
    out = [f"# Run {report['run_id']}", "", f"**Status:** {report['status']}", ""]

    out += ["## Graph", ""]
    for node in report["graph"]:
        needs = f" (needs: {', '.join(node['needs'])})" if node["needs"] else ""
        out.append(f"- {node['id']}{needs}")

    out += ["", "## Nodes", ""]
    for node in report["nodes"]:
        out.append(f"- {node['id']} — {node['status']}{_node_detail(node)}")

    if report["final_output"] is not None:
        final = report["final_output"]
        out += ["", "## Final Output", ""]
        out.append(f"- `{final['node']}.{final['field']}` — {final['status']}")
        out.append(f"- Schema: `{final['schema']}`")
        if final["error"]:
            out.append(f"- Error: {final['error']}")

    out += ["", "## Artifacts", ""]
    artifacts = [(node["id"], path) for node in report["nodes"] for path in node["artifacts"]]
    out += [f"- `{path}` (from {node_id})" for node_id, path in artifacts] or ["_None._"]

    out += ["", "## Errors", ""]
    errors = [node for node in report["nodes"] if node["error"]]
    out += [f"- {node['id']}: {node['error']}" for node in errors] or ["_None._"]

    out += ["", "## Trace", ""]
    for event in report["trace"]:
        out.append(f"- {event['seq']} {event['type']}")
    return "\n".join(out)


def _node_detail(node: dict[str, Any]) -> str:
    """The parenthetical after a node's status: its exit code, or its skip cause.

    A node that ran shows ``(exit N)``; a skipped node shows its cause
    (``(when_false)`` / ``(blocked)`` / ``(all_branches_skipped)``, #7) so the three
    skip reasons read distinctly. A ``blocked`` node also names the upstream that
    withheld it (``(blocked by gate)``, #94) when inferable, at parity with the live
    ``caw run`` message; a node with neither shows nothing.
    """
    if node["exit_status"] is not None:
        return f" (exit {node['exit_status']})"
    if node["cause"] == "blocked" and node.get("blocked_by"):
        return f" (blocked by {node['blocked_by']})"
    if node["cause"]:
        return f" ({node['cause']})"
    return ""


_RENDERERS: dict[ReportFormat, Callable[[dict[str, Any]], str]] = {
    ReportFormat.json: _render_json,
    ReportFormat.jsonl: _render_jsonl,
    ReportFormat.text: _render_text,
    ReportFormat.markdown: _render_markdown,
}


def render_report(run_dir: Path, format: ReportFormat) -> str:
    """Render a report of the Run at ``run_dir`` in the requested format."""
    return _RENDERERS[format](_gather(run_dir))


class GroupReportError(Exception):
    """Raised when a Run Group cannot be reported (absent or with a missing iteration)."""


def _gather_group(group_id: str, base: Path) -> dict[str, Any]:
    """Aggregate a Run Group's per-iteration reports into one result (#15, AC6).

    Reads the authoritative ``group.json`` for the group's status and ordered
    iterations, then renders each iteration's per-run report with the SAME
    single-run gatherer (``_gather``) — so each iteration carries its own
    conclusion (run id, node statuses, artifacts, errors) distinct from its trace
    evidence (events), exactly as a standalone run report does. The Run Group is
    the unit of aggregate reporting (ADR 0002).
    """
    state_path = group_state_path(group_id, base)
    if not state_path.is_file():
        groups_root = state_path.parent.parent
        raise GroupReportError(f"no run group for group id {group_id!r} under {groups_root}")
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    iterations_root = group_iterations_root(group_id, base)
    iterations = []
    for entry in persisted["iterations"]:
        run_dir = iterations_root / entry["run_id"]
        per_run = _gather(run_dir)
        iterations.append(
            {
                "iteration_index": entry["iteration_index"],
                "run_id": per_run["run_id"],
                "status": per_run["status"],
                "nodes": per_run["nodes"],
                "graph": per_run["graph"],
                "trace": per_run["trace"],
                "final_output": per_run["final_output"],
            }
        )
    return {
        "group_id": persisted["group_id"],
        "status": persisted["status"],
        # A controller may record a top-level result on group.json (the tournament's
        # final promoted winner); carry it so the aggregate report is as informative as
        # the controller's own run output. Absent (e.g. loop/verify) it stays None.
        "winner": persisted.get("winner"),
        "iteration_count": len(iterations),
        "iterations": iterations,
    }


def _render_group_json(report: dict[str, Any]) -> str:
    """Machine-readable aggregate: group status + final winner + per-iteration conclusion."""
    contract = {
        "group_id": report["group_id"],
        "status": report["status"],
        # The controller's top-level result (the tournament's final winner), carried so
        # the aggregate report carries it; always present (``null`` for loop/verify).
        "winner": report["winner"],
        "iteration_count": report["iteration_count"],
        "iterations": [
            {
                key: iteration[key]
                for key in (
                    "iteration_index",
                    "run_id",
                    "status",
                    "nodes",
                    "trace",
                    "final_output",
                )
                if key != "final_output" or iteration[key] is not None
            }
            for iteration in report["iterations"]
        ],
    }
    return json.dumps(contract, indent=2)


def _render_group_jsonl(report: dict[str, Any]) -> str:
    """Line-delimited aggregate: a group record, then per-iteration conclusion + events."""
    lines = [
        json.dumps(
            {
                "record": "group",
                "group_id": report["group_id"],
                "status": report["status"],
                "winner": report["winner"],
                "iteration_count": report["iteration_count"],
            }
        )
    ]
    for iteration in report["iterations"]:
        lines.append(
            json.dumps(
                {
                    "record": "iteration",
                    "iteration_index": iteration["iteration_index"],
                    "run_id": iteration["run_id"],
                    "status": iteration["status"],
                    "nodes": iteration["nodes"],
                    **(
                        {"final_output": iteration["final_output"]}
                        if iteration["final_output"] is not None
                        else {}
                    ),
                }
            )
        )
        lines += [
            json.dumps(
                {"record": "event", "iteration_index": iteration["iteration_index"], **event}
            )
            for event in iteration["trace"]
        ]
    return "\n".join(lines)


def _render_group_text(report: dict[str, Any]) -> str:
    """Plain text: the group status, the final winner (when any), then each conclusion."""
    lines = [
        f"group {report['group_id']}: {report['status']} ({report['iteration_count']} iterations)"
    ]
    if report["winner"] is not None:
        lines.append(f"winner: {report['winner']}")
    for iteration in report["iterations"]:
        lines.append(
            f"  iteration {iteration['iteration_index']} ({iteration['run_id']}): "
            f"{iteration['status']}"
        )
        for node in iteration["nodes"]:
            lines.append(f"    {node['id']}: {node['status']}{_node_detail(node)}")
        if iteration["final_output"] is not None:
            final = iteration["final_output"]
            lines.append("    final output:")
            lines.append(
                f"      {final['node']}.{final['field']}: {final['status']} "
                f"(schema: {final['schema']})"
            )
            if final["error"]:
                lines.append(f"      error: {final['error']}")
    return "\n".join(lines)


def _render_group_markdown(report: dict[str, Any]) -> str:
    """Markdown: the group heading and status, then a section per iteration."""
    out = [
        f"# Run Group {report['group_id']}",
        "",
        f"**Status:** {report['status']}",
    ]
    if report["winner"] is not None:
        out.append(f"**Winner:** {report['winner']}")
    out += [
        f"**Iterations:** {report['iteration_count']}",
        "",
    ]
    for iteration in report["iterations"]:
        out += [f"## Iteration {iteration['iteration_index']}", ""]
        out.append(f"**Run:** {iteration['run_id']} — {iteration['status']}")
        out += ["", "### Nodes", ""]
        for node in iteration["nodes"]:
            out.append(f"- {node['id']} — {node['status']}{_node_detail(node)}")
        if iteration["final_output"] is not None:
            final = iteration["final_output"]
            out += ["", "### Final Output", ""]
            out.append(f"- `{final['node']}.{final['field']}` — {final['status']}")
            out.append(f"- Schema: `{final['schema']}`")
            if final["error"]:
                out.append(f"- Error: {final['error']}")
        out.append("")
    return "\n".join(out)


_GROUP_RENDERERS: dict[ReportFormat, Callable[[dict[str, Any]], str]] = {
    ReportFormat.json: _render_group_json,
    ReportFormat.jsonl: _render_group_jsonl,
    ReportFormat.text: _render_group_text,
    ReportFormat.markdown: _render_group_markdown,
}


def render_group_report(group_id: str, base: Path, format: ReportFormat) -> str:
    """Render an aggregate report of a Run Group in the requested format (#15, AC6)."""
    return _GROUP_RENDERERS[format](_gather_group(group_id, base))
