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

from caw.state import StateStore


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


def _read_graph(run_dir: Path) -> list[dict[str, Any]]:
    """The Run's nodes and dependency edges, read from the persisted snapshot.

    Reads the normalized snapshot rather than re-validating the Workflow, so the
    report reflects exactly the graph that ran, in declaration order.
    """
    snapshot = json.loads((run_dir / "workflow.normalized.json").read_text(encoding="utf-8"))
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
        failed = node_status not in {"succeeded", "skipped"}
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
    status, nodes = _conclusion_nodes(run_dir)
    return {
        "run_id": run_dir.name,
        "status": status,
        "nodes": nodes,
        "graph": _read_graph(run_dir),
        "trace": _read_trace(run_dir),
    }


def _render_json(report: dict[str, Any]) -> str:
    """Machine-readable conclusion + trace (the confirmed top-level contract, #12)."""
    contract = {key: report[key] for key in ("run_id", "status", "nodes", "trace")}
    return json.dumps(contract, indent=2)


def _render_jsonl(report: dict[str, Any]) -> str:
    """Line-delimited stream: a tagged conclusion record, then one record per event."""
    summary = {key: report[key] for key in ("run_id", "status", "nodes")}
    lines = [json.dumps({"record": "conclusion", **summary})]
    lines += [json.dumps({"record": "event", **event}) for event in report["trace"]]
    return "\n".join(lines)


def _render_text(report: dict[str, Any]) -> str:
    """Plain text: the conclusion first, then the trace under a distinct heading."""
    lines = [f"run {report['run_id']}: {report['status']}", "nodes:"]
    for node in report["nodes"]:
        lines.append(f"  {node['id']}: {node['status']}{_node_detail(node)}")
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
    skip reasons read distinctly; a node with neither shows nothing.
    """
    if node["exit_status"] is not None:
        return f" (exit {node['exit_status']})"
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
