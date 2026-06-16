"""Real-agent-CLI e2e: `caw report` over a run produced by the real Agent CLI (#12).

The Reporter renders from persisted State and Events only; its logic is deterministic
and fully covered offline by the report-seam suite. What depends on the REAL Agent CLI
is the SHAPE of what a real agent Node persists — its ``structured_output``, stdout, and
exit status — so this e2e runs a real structured agent Node (the adapter the selected
``CAW_E2E_AGENT`` maps to) through ``execute_run`` and asserts the report surfaces that
real conclusion. Structure, not exact words (decision #4); the agent is selected by
``CAW_E2E_AGENT`` (default ``claude``) and the suite FAILS (never skips) when the
selected CLI is absent.
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
_NODE_ID = "agent"


def _structured_agent_workflow(agent: str, schema: Path) -> Workflow:
    """A one-node structured agent Workflow targeting the selected agent's Adapter."""
    raw = {
        "name": "e2e-report",
        "version": 1,
        "nodes": [
            {
                "id": _NODE_ID,
                "kind": "agent",
                "timeout": _NODE_TIMEOUT_S,
                "inputs": {
                    "adapter": harness.adapter_for_agent(agent),
                    "prompt": "Compute 2 + 2. Put the result in the 'answer' field as an integer.",
                    "output_schema": str(schema),
                    "env": list(harness.agent_env_names()),
                },
            }
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
async def test_report_renders_a_real_agent_runs_conclusion_and_trace(
    agent: str, tmp_path: Path
) -> None:
    # `caw report` over a run produced by the REAL agent CLI: the report renders from
    # persisted State only and surfaces the real agent node's conclusion — its
    # succeeded status, exit 0, and structured_output with the contracted shape — plus
    # the agent node finishing in the trace. Value asserted by structure, not words.
    harness.require_agent_cli(agent)  # FAIL (not skip) when the selected CLI is absent
    schema = tmp_path / "answer.schema.json"
    # `additionalProperties: false` and a fully-listed `required` keep the schema valid
    # under codex's strict (OpenAI structured-output) mode and are harmless for claude,
    # so the same structured workflow runs under either CAW_E2E_AGENT (#11 symmetry).
    schema.write_text(
        json.dumps(
            {
                "type": "object",
                "properties": {"answer": {"type": "integer"}},
                "required": ["answer"],
                "additionalProperties": False,
            }
        ),
        encoding="utf-8",
    )
    workflow = _structured_agent_workflow(agent, schema)
    runs_root = tmp_path / "runs"

    async def do_run() -> RunResult:
        return await execute_run(workflow, runs_root, registry=AdapterRegistry())

    result = await harness.run_with_transient_retry(do_run)
    assert result.succeeded, f"structured run failed: {_why(result)}"

    run_dir = runs_root / result.run_id
    report: dict[str, Any] = json.loads(render_report(run_dir, ReportFormat.json))

    # Conclusion read back from persisted State: the real node, its exit, its output.
    assert report["run_id"] == result.run_id
    assert report["status"] == "succeeded"
    node = next(item for item in report["nodes"] if item["id"] == _NODE_ID)
    assert node["status"] == "succeeded"
    assert node["exit_status"] == 0
    assert isinstance(node["structured_output"], dict)
    assert isinstance(node["structured_output"].get("answer"), int)

    # Trace evidence: the real run's events, including the agent node finishing.
    assert any(
        event["type"] == "node_finished" and event["data"]["node_id"] == _NODE_ID
        for event in report["trace"]
    )

    # Markdown renders the same real run without error (the graph and the agent node).
    markdown = render_report(run_dir, ReportFormat.markdown)
    assert f"# Run {result.run_id}" in markdown
    assert _NODE_ID in markdown
