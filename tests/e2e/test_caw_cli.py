"""Real ``caw run`` / ``caw resume`` CLI e2e with a real agent node (#86).

These drive the actual user entrypoints — ``caw run`` and ``caw resume`` through
Typer's ``CliRunner`` — with a real ``claude.print`` agent node, so the
CLI -> kernel -> real agent -> State path is exercised end to end. (The graph-run
e2e in ``test_claude_print_graph_runs.py`` call ``execute_run`` directly; this file
closes the CLI-entrypoint gap.) Part of the living e2e suite, co-weighted with the
mock suite that covers what a fixture can verify offline.

LIMITATION (reported, not worked around): a ``when`` predicate cannot today gate on
a real agent's ``structured_output``. The shipped algebra references a WHOLE field
(``stdout`` / ``exit_status`` / ``structured_output``) compared to a SCALAR value,
with ``contains`` valid only on ``stdout`` (``model.py`` ``PredicateField`` /
``Predicate.value`` / ``_STRING_PREDICATE_FIELDS``); there is no sub-field path, and
``claude`` rejects a scalar top-level ``--json-schema`` (it returns ``is_error``).
So the strongest model-driven gate on a real agent is ``stdout``/``contains``, used
here — it still exercises a downstream node's fate being decided by what the real
agent actually emitted.
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


def _single_run_dir(tmp_path: Path) -> Path:
    """The one run directory ``caw run`` materialized under ``<cwd>/.caw/runs``."""
    run_dirs = list((tmp_path / ".caw" / "runs").iterdir())
    assert len(run_dirs) == 1, f"expected exactly one run dir, got {run_dirs}"
    return run_dirs[0]


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
    # produces stdout, and two downstream shell nodes are gated by `when` on that real
    # stdout — one matches (must run), one does not (must skip). This covers the CLI
    # entrypoint + multi-node data flow + a downstream fate decided by real agent
    # output, all through `caw run`, asserted on State (not model text).
    harness.require_agent_cli(agent)
    workflow_file = write_workflow_data(
        {
            "name": "e2e-graph",
            "version": 1,
            "nodes": [
                _agent_node(
                    "answer",
                    agent,
                    prompt="Reply with exactly the single uppercase word FOUR and nothing else.",
                ),
                {
                    "id": "on_match",
                    "kind": "shell",
                    "needs": ["answer"],
                    "when": {
                        "ref": {"node": "answer", "field": "stdout"},
                        "op": "contains",
                        "value": "FOUR",
                    },
                    "inputs": {"command": "echo matched"},
                },
                {
                    "id": "on_other",
                    "kind": "shell",
                    "needs": ["answer"],
                    "when": {
                        "ref": {"node": "answer", "field": "stdout"},
                        "op": "contains",
                        "value": "FIVE",
                    },
                    "inputs": {"command": "echo nope"},
                },
            ],
        }
    )
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["run", str(workflow_file)])

    assert result.exit_code == 0, f"caw run failed: {result.output}"
    run_dir = _single_run_dir(tmp_path)
    run_id = run_dir.name
    with StateStore(run_dir / "state.sqlite") as state:
        statuses = state.node_statuses(run_id)
        answer_output = state.node_output(run_id, "answer")
    assert statuses["answer"] == "succeeded", "the real agent node ran and succeeded"
    assert statuses["on_match"] == "succeeded", "the gate matching the real agent stdout ran"
    assert statuses["on_other"] == "skipped", "the non-matching gate was skipped (when_false)"
    assert answer_output is not None
    assert "FOUR" in answer_output["stdout"], "the gate decision is driven by real agent output"


def test_caw_resume_reuses_a_succeeded_real_agent_node(
    agent: str,
    write_workflow_data: Callable[[dict[str, Any]], Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Resume must NOT re-invoke a real agent node that already succeeded. A real agent
    # node `gen` succeeds (one real call), then a downstream shell `boom` fails. `caw
    # resume` re-runs only the incomplete node: `gen` is seeded satisfied (attempt
    # stays 1 — no second token spend), `boom` re-runs (attempt 2). Proven from State.
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

    first = runner.invoke(app, ["run", str(workflow_file)])

    assert first.exit_code == 1, f"the first run must fail at boom: {first.output}"
    run_dir = _single_run_dir(tmp_path)
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
