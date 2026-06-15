"""Real ``caw run`` / ``caw resume`` CLI e2e with a real agent node (#86).

These drive the actual user entrypoints — ``caw run`` and ``caw resume`` through
Typer's ``CliRunner`` — with a real ``claude.print`` agent node, so the
CLI -> kernel -> real agent -> State path is exercised end to end. (The graph-run
e2e in ``test_claude_print_graph_runs.py`` call ``execute_run`` directly; this file
closes the CLI-entrypoint gap.) Part of the living e2e suite, co-weighted with the
mock suite that covers what a fixture can verify offline.

The multi-node test gates a downstream node on the agent node's ``exit_status`` — a
STRUCTURAL field of its output — so the assertion is deterministic and free of
model-text matching (#86 decision #4). Gating on the agent's CONTENT (a sub-field of
``structured_output``) is not expressible today: the predicate algebra references a
WHOLE field against a SCALAR value with no sub-path (``model.py`` ``PredicateField`` /
``Predicate.value``), tracked in #89 — so content-driven branching is deferred there.

Real agent calls go through ``harness.run_cli_with_transient_retry`` so a transient
network / 5xx / rate-limit blip is retried (decision #6), like the graph-run e2e; the
helper also returns the latest run dir, resolving the several a retry materializes.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from caw.cli import app
from caw.state import StateStore
from e2e import harness

runner = CliRunner()

# A generous per-Node budget so ordinary model latency never trips the kernel timeout.
_AGENT_TIMEOUT_S = 300.0


def _agent_node(node_id: str, agent: str, *, prompt: str) -> dict[str, Any]:
    """A one-node agent spec targeting the selected agent's adapter.

    Declares the ambient env-var NAMES (ADR 0006 allow-list) so the real CLI inherits
    the developer's auth/config, plus a generous timeout for model latency.
    """
    return {
        "id": node_id,
        "kind": "agent",
        "needs": [],
        "timeout": _AGENT_TIMEOUT_S,
        "inputs": {
            "adapter": harness.adapter_for_agent(agent),
            "prompt": prompt,
            "env": list(harness.agent_env_names()),
        },
    }


def test_caw_run_drives_a_real_agent_graph_with_when_gating(
    agent: str,
    write_workflow_data: Callable[[dict[str, Any]], Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `caw run` (the real CLI entrypoint) over a multi-node graph: a real agent node
    # runs, and two downstream shell nodes are gated by `when` on the agent node's
    # `exit_status` — one matches (must run), one does not (must skip). Covers the CLI
    # entrypoint + multi-node data flow + a downstream fate decided by a real agent
    # node's (structural) output, asserted on State. exit_status is deterministic, so
    # there is NO model-text matching (#86 decision #4); content gating awaits #89.
    harness.require_agent_cli(agent)
    workflow_file = write_workflow_data(
        {
            "name": "e2e-graph",
            "version": 1,
            "nodes": [
                _agent_node("answer", agent, prompt="Reply with the single word OK."),
                {
                    "id": "on_success",
                    "kind": "shell",
                    "needs": ["answer"],
                    "when": {
                        "ref": {"node": "answer", "field": "exit_status"},
                        "op": "equals",
                        "value": 0,
                    },
                    "inputs": {"command": "echo ran"},
                },
                {
                    "id": "on_failure",
                    "kind": "shell",
                    "needs": ["answer"],
                    "when": {
                        "ref": {"node": "answer", "field": "exit_status"},
                        "op": "equals",
                        "value": 1,
                    },
                    "inputs": {"command": "echo nope"},
                },
            ],
        }
    )
    monkeypatch.chdir(tmp_path)
    runs_root = tmp_path / ".caw" / "runs"

    result, run_dir = harness.run_cli_with_transient_retry(
        lambda: runner.invoke(app, ["run", str(workflow_file)]), runs_root
    )

    assert result.exit_code == 0, f"caw run failed: {result.output}"
    assert run_dir is not None
    with StateStore(run_dir / "state.sqlite") as state:
        statuses = state.node_statuses(run_dir.name)
    assert statuses["answer"] == "succeeded", "the real agent node ran and succeeded"
    assert statuses["on_success"] == "succeeded", "the gate on exit_status == 0 ran"
    assert statuses["on_failure"] == "skipped", (
        "the gate on exit_status == 1 was skipped (when_false)"
    )


def test_caw_resume_reuses_a_succeeded_real_agent_node(
    agent: str,
    write_workflow_data: Callable[[dict[str, Any]], Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Resume must NOT re-invoke a real agent node that already succeeded. A real agent
    # node `gen` succeeds (one real call), then a downstream shell `boom` fails. `caw
    # resume` re-runs only the incomplete node: `gen` stays at attempt 1 (no second
    # token spend), `boom` re-runs (attempt 2). Proven from State. The first `caw run`
    # is wrapped in CLI transient retry (it makes the real agent call); the resume is
    # not — resume re-runs only `boom` (shell), so it makes no agent call.
    harness.require_agent_cli(agent)
    workflow_file = write_workflow_data(
        {
            "name": "e2e-resume",
            "version": 1,
            "nodes": [
                _agent_node("gen", agent, prompt="Reply with the single word OK."),
                {
                    "id": "boom",
                    "kind": "shell",
                    "needs": ["gen"],
                    "inputs": {"command": "exit 1"},
                },
            ],
        }
    )
    monkeypatch.chdir(tmp_path)
    runs_root = tmp_path / ".caw" / "runs"

    first, run_dir = harness.run_cli_with_transient_retry(
        lambda: runner.invoke(app, ["run", str(workflow_file)]), runs_root
    )

    assert first.exit_code == 1, f"the first run must fail at boom: {first.output}"
    assert run_dir is not None
    run_id = run_dir.name
    with StateStore(run_dir / "state.sqlite") as state:
        statuses_before = state.node_statuses(run_id)
        attempts_before = state.max_attempt_per_node(run_id)
    assert statuses_before["gen"] == "succeeded"
    assert statuses_before["boom"] == "failed"
    assert attempts_before["gen"] == 1

    resumed = runner.invoke(app, ["resume", run_id])

    assert resumed.exit_code == 1, f"resume re-runs boom, which still fails: {resumed.output}"
    with StateStore(run_dir / "state.sqlite") as state:
        statuses_after = state.node_statuses(run_id)
        attempts_after = state.max_attempt_per_node(run_id)
    assert attempts_after["gen"] == 1, "resume did NOT re-invoke the succeeded real agent node"
    assert attempts_after["boom"] == 2, "resume re-ran only the incomplete shell node"
    assert statuses_after["gen"] == "succeeded"
