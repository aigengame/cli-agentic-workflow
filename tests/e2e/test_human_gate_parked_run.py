"""Real-agent-CLI e2e: a real run parks at a human_gate and `caw report` renders it (#10).

Deferred here from #90: the Reporter renders parked/awaiting status-agnostically (covered
offline by the report-seam suite), so what this e2e adds is the REAL flow — a real agent
Node runs through ``execute_run``, the run then parks at a downstream ``human_gate`` (ADR
0010), and the report surfaces the parked run and the awaiting gate from persisted State.
The agent is selected by ``CAW_E2E_AGENT`` (default ``claude``); the suite FAILS (never
skips) when the selected CLI is absent.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from caw.adapter import AdapterRegistry
from caw.executor import RunResult, execute_run
from caw.model import Workflow, normalize_workflow
from caw.report import ReportFormat, render_report
from e2e import harness

# A generous per-Node wall-clock budget so ordinary model latency never trips the
# kernel's timeout; a genuine hang still fails rather than blocking forever.
_NODE_TIMEOUT_S = 300.0
_AGENT_ID = "agent"
_GATE_ID = "gate"


def _gated_agent_workflow(agent: str) -> Workflow:
    """A real agent Node followed by a human_gate: agent -> gate (deploy is gated)."""
    inputs: dict[str, Any] = {
        "adapter": harness.adapter_for_agent(agent),
        "prompt": "Reply with a one-word greeting.",
        "env": list(harness.agent_env_names()),
    }
    run_args = harness.agent_run_args(agent)
    if run_args:
        inputs["args"] = list(run_args)
    raw = {
        "name": "e2e-gated",
        "version": 1,
        "nodes": [
            {"id": _AGENT_ID, "kind": "agent", "timeout": _NODE_TIMEOUT_S, "inputs": inputs},
            {
                "id": _GATE_ID,
                "kind": "human_gate",
                "needs": [_AGENT_ID],
                "inputs": {"prompt": "Approve the deploy?"},
            },
        ],
    }
    return normalize_workflow(raw, source="<e2e>")


def _why(result: RunResult) -> str:
    """A debuggable reason string surfacing failed Nodes' stderr in an assertion."""
    return "; ".join(
        f"{node.node_id}: {node.status}: {node.stderr.strip()}"
        for node in result.node_results
        if not node.succeeded
    )


@pytest.mark.asyncio
async def test_a_real_agent_run_parks_at_a_human_gate_and_reports_parked(
    agent: str, tmp_path: Path
) -> None:
    # A real agent Node runs, then the run parks at the downstream human_gate: the run
    # is `parked`, the agent node `succeeded`, the gate `awaiting`, and `caw report`
    # surfaces all of that from persisted State in JSON and Markdown.
    harness.require_agent_cli(agent)  # FAIL (not skip) when the selected CLI is absent
    workflow = _gated_agent_workflow(agent)
    runs_root = tmp_path / "runs"

    async def do_run() -> RunResult:
        return await execute_run(workflow, runs_root, registry=AdapterRegistry())

    result = await harness.run_with_transient_retry(do_run)

    assert result.status == "parked", f"expected a parked run: {_why(result)}"
    assert result.awaiting_node_ids == (_GATE_ID,)

    run_dir = runs_root / result.run_id
    report: dict[str, Any] = json.loads(render_report(run_dir, ReportFormat.json))

    assert report["status"] == "parked"
    agent_node = next(item for item in report["nodes"] if item["id"] == _AGENT_ID)
    assert agent_node["status"] == "succeeded", "the real agent node ran before the gate"
    gate_node = next(item for item in report["nodes"] if item["id"] == _GATE_ID)
    assert gate_node["status"] == "awaiting"
    assert gate_node["error"] is None, "an awaiting gate is not a failure"
    assert any(
        event["type"] == "gate_awaiting" and event["data"]["node_id"] == _GATE_ID
        for event in report["trace"]
    )

    # Markdown renders the same parked run without error: the awaiting gate is visible.
    markdown = render_report(run_dir, ReportFormat.markdown)
    assert f"# Run {result.run_id}" in markdown
    assert _GATE_ID in markdown
    assert "awaiting" in markdown
