"""Real dual-adapter e2e for the fan-out-synthesis sample (#14, #86).

The fan-out-synthesis sample (``examples/fanout-synthesis/``) is the project's first
complete end-to-end agent sample. The reference variant fans the SAME task out to a
``claude.print`` branch AND a ``codex.exec`` branch in PARALLEL, then a ``synthesize``
node — gated on BOTH branches — combines their real answers (PRD decided 2026-06-11;
issue #14 AC1). This test runs the SHIPPED ``fanout-synthesis.real.yaml`` sample
end-to-end against BOTH real CLIs and asserts each branch reached its OWN adapter, the
kernel validated each real output against its Output Contract, and the synthesize node
consumed both — what the offline mock variant (``tests/test_fanout_cli_seam.py``)
cannot prove because it never spawns a real CLI.

Unlike the agent-NEUTRAL e2e tests (which run ONE adapter chosen by ``CAW_E2E_AGENT``),
this test is intrinsically DUAL-adapter: the whole point of the sample is both adapters
in one run. It therefore REQUIRES both the ``claude`` and the ``codex`` CLIs and FAILS
(never skips) if either is absent, consistent with the e2e-first norm (#86). CI runs
``pytest -m "not e2e"`` and so excludes this; it is a local-only proof.

Token-frugal by construction: TWO real branch calls plus ONE synthesis call (three
total). The sample's ``output_schema`` files use ``additionalProperties: false`` so they
are valid under codex's strict structured-output mode and harmless for claude (#11
symmetry). Assertions are contract/structure-based, never free-text (decision #4).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from caw.adapter import AdapterRegistry
from caw.config import load_workflow_file
from caw.executor import RunResult, execute_run
from caw.model import AgentNodeInputs, normalize_workflow
from caw.report import ReportFormat, render_report
from caw.state import StateStore
from e2e import harness

# The shipped real sample, located relative to the repo root (this file lives at
# tests/e2e/test_fanout_synthesis_runs.py).
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SAMPLE_DIR = _REPO_ROOT / "examples" / "fanout-synthesis"
_REAL_SAMPLE = _SAMPLE_DIR / "fanout-synthesis.real.yaml"

_CLAUDE_BRANCH = "claude_branch"
_CODEX_BRANCH = "codex_branch"
_SYNTH_ID = "synthesize"


def _why(result: RunResult) -> str:
    """A debuggable reason string surfacing failed Nodes' stderr in an assertion."""
    return "; ".join(
        f"{node.node_id}: {node.status}: {node.stderr.strip()}"
        for node in result.node_results
        if not node.succeeded
    )


def _augment_runtime_inputs(raw: dict[str, Any]) -> None:
    """Inject the runtime-only inputs the static sample deliberately omits, in place.

    The sample file declares the graph SHAPE (the two adapters, the same prompt, the
    synthesize join) but not two machine/run-context concerns that cannot live in a
    portable file (documented in the sample's own header):

    * ``env`` — an agent Node receives ONLY its declared env NAMES (values stay out of
      State/Events; ADR 0006). A real CLI needs the developer's ambient auth/config, so
      every agent Node gets the full ambient allow-list — this is a local run against
      the developer's own authenticated CLIs, where passing the ambient env is the point.
    * codex ``args`` — ``codex exec`` needs ``--skip-git-repo-check`` and a
      non-interactive ``--sandbox`` to run unattended from a tmp dir; claude needs none.
      The codex branch therefore gets codex's headless run args; claude's branches do not.

    This keeps the e2e running the SAMPLE's actual graph (its prompts, its adapters, its
    join), supplying only what a static file cannot carry.
    """
    env_names = list(harness.agent_env_names())
    codex_args = list(harness.agent_run_args("codex"))
    for node in raw["nodes"]:
        inputs = node["inputs"]
        inputs["env"] = env_names
        if inputs["adapter"] == harness.adapter_for_agent("codex"):
            inputs["args"] = codex_args


@pytest.mark.asyncio
async def test_real_sample_fans_the_same_task_to_claude_and_codex_then_synthesizes(
    tmp_path: Path,
) -> None:
    # The shipped real sample, run end-to-end against BOTH real CLIs: branch 1 reaches
    # claude.print, branch 2 reaches codex.exec, BOTH with the IDENTICAL prompt (the
    # same task fanned out), and the synthesize node — gated on BOTH branches — runs
    # last against a real CLI too, consuming both real outputs. This is the dual-adapter
    # path the offline mock variant cannot prove.
    harness.require_agent_cli("claude")  # FAIL (not skip) when either CLI is absent
    harness.require_agent_cli("codex")

    # Load the SHIPPED sample so the e2e runs its real graph; anchor its relative
    # output_schema paths to the sample's own directory, exactly as `caw run` does (#64).
    raw = load_workflow_file(_REAL_SAMPLE)
    _augment_runtime_inputs(raw)
    workflow = normalize_workflow(raw, source=str(_REAL_SAMPLE), base_dir=_SAMPLE_DIR)

    by_id = {node.id: node for node in workflow.nodes}

    # The sample IS a dual-adapter fan-out-synthesis: two independent branches on the
    # TWO different real adapters, with the IDENTICAL prompt, joined by a synthesize node
    # that needs BOTH. Asserted on the loaded sample BEFORE anything runs.
    claude_branch = by_id[_CLAUDE_BRANCH]
    codex_branch = by_id[_CODEX_BRANCH]
    synth = by_id[_SYNTH_ID]
    claude_inputs = claude_branch.inputs
    codex_inputs = codex_branch.inputs
    assert isinstance(claude_inputs, AgentNodeInputs)
    assert isinstance(codex_inputs, AgentNodeInputs)
    assert claude_inputs.adapter == "claude.print"
    assert codex_inputs.adapter == "codex.exec"
    assert claude_branch.needs == () and codex_branch.needs == (), (
        "the fan-out branches are independent (no needs)"
    )
    assert claude_inputs.prompt == codex_inputs.prompt, (
        "the SAME task is fanned out to both adapters"
    )
    assert sorted(synth.needs) == sorted((_CLAUDE_BRANCH, _CODEX_BRANCH)), (
        "the synthesize node fans in BOTH branches"
    )

    runs_root = tmp_path / "runs"

    async def do_run() -> RunResult:
        return await execute_run(workflow, runs_root, registry=AdapterRegistry())

    result = await harness.run_with_transient_retry(do_run)

    assert result.succeeded, f"dual-adapter fan-out-synthesis run failed: {_why(result)}"
    with StateStore(runs_root / result.run_id / "state.sqlite") as state:
        # Each real branch persisted a contracted structured output (a `ranked` list).
        for branch_id in (_CLAUDE_BRANCH, _CODEX_BRANCH):
            output = state.node_output(result.run_id, branch_id)
            assert output is not None, f"branch {branch_id} output is persisted to State"
            structured = output["structured_output"]
            # Structure, not exact words (robust to LLM nondeterminism, decision #4).
            assert isinstance(structured, dict)
            assert isinstance(structured.get("ranked"), list)
        # The synthesize node ran last (gated on both branches) and persisted its output.
        synth_output = state.node_output(result.run_id, _SYNTH_ID)
    assert synth_output is not None, "the synthesize node's output is persisted to State"
    synth_structured = synth_output["structured_output"]
    assert isinstance(synth_structured, dict)
    assert isinstance(synth_structured.get("recommendation"), str)
    assert isinstance(synth_structured.get("ranked"), list)

    report = render_report(runs_root / result.run_id, ReportFormat.markdown)
    assert "## Final Output" in report
    assert f"`{_SYNTH_ID}.structured_output` — valid" in report
    assert "## Artifacts" in report
    assert "## Trace" in report
    assert "run_started" in report
