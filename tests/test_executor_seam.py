"""Executor-seam tests: drive execute_run directly on the asyncio Engine Backend."""

import asyncio
import json
import os
import signal
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from caw.adapter import Adapter, AdapterRegistry, AgentInvocation, AgentResult
from caw.executor import ResumeError, execute_run, resume_run
from caw.model import Workflow, normalize_workflow

ShellNodeSpec = str | tuple[str, str, list[str]]


def shell_workflow(*nodes: ShellNodeSpec) -> Workflow:
    """Build a shell Workflow from node specs.

    A bare command string becomes an auto-numbered ``nodeN`` with no needs; a
    ``(id, command, needs)`` triple expresses an explicit id and dependency
    edges, so dependency-shaped tests need no inline raw dicts.
    """
    raw_nodes: list[dict[str, Any]] = []
    for index, spec in enumerate(nodes, start=1):
        node_id, command, needs = (f"node{index}", spec, []) if isinstance(spec, str) else spec
        raw_nodes.append(
            {"id": node_id, "kind": "shell", "needs": needs, "inputs": {"command": command}}
        )
    raw: dict[str, Any] = {"name": "sample", "version": 1, "nodes": raw_nodes}
    return normalize_workflow(raw, source="<test>")


def conditional_workflow(*nodes: dict[str, Any]) -> Workflow:
    """Build a shell Workflow from raw node dicts carrying `when` / `join` (#7).

    Each node dict is a full shell-node spec — at least ``id`` and an
    ``inputs.command`` — so the predicate/join tests can declare a `when`
    predicate or a `join` policy inline without an outer raw-workflow scaffold.
    """
    raw: dict[str, Any] = {"name": "sample", "version": 1, "nodes": list(nodes)}
    return normalize_workflow(raw, source="<test>")


def shell(node_id: str, command: str, **fields: Any) -> dict[str, Any]:
    """A shell-node dict for ``conditional_workflow``, with optional needs/when/join."""
    return {"id": node_id, "kind": "shell", "inputs": {"command": command}, **fields}


def single_run_dir(runs_root: Path) -> Path:
    run_dirs = list(runs_root.iterdir())
    assert len(run_dirs) == 1
    return run_dirs[0]


def state_rows(run_dir: Path, query: str) -> list[dict[str, Any]]:
    connection = sqlite3.connect(run_dir / "state.sqlite")
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute(query)]
    finally:
        connection.close()


def read_events(run_dir: Path) -> list[dict[str, Any]]:
    lines = (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


def policy_shell_workflow(node_id: str, command: str, **policy: Any) -> Workflow:
    """A single-shell-node Workflow carrying per-Node failure-semantics policy.

    ``policy`` passes ``retries`` / ``timeout`` straight onto the node so the
    failure-semantics tests can declare a budget without an inline raw dict.
    """
    raw: dict[str, Any] = {
        "name": "sample",
        "version": 1,
        "nodes": [{"id": node_id, "kind": "shell", "inputs": {"command": command}, **policy}],
    }
    return normalize_workflow(raw, source="<test>")


@pytest.mark.asyncio
async def test_a_node_exceeding_its_timeout_is_terminated_and_recorded_timed_out(
    tmp_path: Path,
) -> None:
    # Acceptance criterion #6.2: a Node whose wall-clock exceeds its `timeout` is
    # terminated and recorded with a status DISTINCT from a non-zero exit, so a
    # timeout is diagnosable as a timeout — not conflated with an ordinary
    # failure. The 0.2s budget against a 30s sleep makes the timeout deterministic.
    workflow = policy_shell_workflow("slow", "sleep 30", timeout=0.2)

    result = await execute_run(workflow, tmp_path / "runs")

    assert not result.succeeded, "a timed-out node fails the run"
    run_dir = single_run_dir(tmp_path / "runs")
    (node,) = state_rows(run_dir, "SELECT * FROM node")
    assert node["status"] == "timed_out", "the node is recorded timed_out, not failed"


@pytest.mark.asyncio
async def test_a_node_with_retries_reattempts_on_failure_and_records_each_attempt(
    tmp_path: Path,
) -> None:
    # Acceptance criterion #6.1: a Node with `retries` re-attempts on failure and
    # the attempt history is recorded in State. The command fails on its first
    # run (no marker yet) and succeeds on its second (marker now exists), so a
    # node with retries=1 ultimately succeeds — and BOTH attempts are recorded as
    # distinct rows in the attempt table, the durable Attempt history.
    marker = tmp_path / "marker"
    command = f"if [ -e {marker} ]; then exit 0; else touch {marker}; exit 7; fi"
    workflow = policy_shell_workflow("flaky", command, retries=1)

    result = await execute_run(workflow, tmp_path / "runs")

    assert result.succeeded, "the second attempt succeeded, so the run succeeds"
    run_dir = single_run_dir(tmp_path / "runs")
    (node,) = state_rows(run_dir, "SELECT * FROM node")
    assert node["status"] == "succeeded", "the node's terminal status is its last attempt's"
    attempts = state_rows(
        run_dir,
        "SELECT attempt, exit_status FROM attempt WHERE node_id = 'flaky' ORDER BY attempt",
    )
    assert [(a["attempt"], a["exit_status"]) for a in attempts] == [(1, 7), (2, 0)], (
        "both attempts are recorded: attempt 1 failed (exit 7), attempt 2 succeeded"
    )
    (terminal,) = result.node_results
    assert terminal.attempt == 2, (
        "the terminal NodeResult names the real attempt number, not a misleading 1"
    )


@pytest.mark.asyncio
async def test_retries_are_exhausted_then_the_node_fails_and_skips_its_dependents(
    tmp_path: Path,
) -> None:
    # The retry budget is bounded: a Node that fails EVERY Attempt exhausts its
    # retries, goes terminal-failed, and skips its transitive dependents exactly
    # as a non-retried failure does (#4 semantics preserved). retries=2 means
    # three Attempts total, all exit 7; the dependent is never run.
    marker = tmp_path / "deployed"
    raw: dict[str, Any] = {
        "name": "sample",
        "version": 1,
        "nodes": [
            {"id": "build", "kind": "shell", "retries": 2, "inputs": {"command": "exit 7"}},
            {
                "id": "deploy",
                "kind": "shell",
                "needs": ["build"],
                "inputs": {"command": f"touch {marker}"},
            },
        ],
    }
    workflow = normalize_workflow(raw, source="<test>")

    result = await execute_run(workflow, tmp_path / "runs")

    assert not result.succeeded
    assert not marker.exists(), "the dependent of an exhausted-retry failure never runs"
    run_dir = single_run_dir(tmp_path / "runs")
    nodes = {row["node_id"]: row["status"] for row in state_rows(run_dir, "SELECT * FROM node")}
    assert nodes == {"build": "failed", "deploy": "skipped"}
    attempts = state_rows(run_dir, "SELECT attempt FROM attempt WHERE node_id = 'build'")
    assert len(attempts) == 3, "retries=2 yields exactly three recorded attempts"


@pytest.mark.asyncio
async def test_a_timed_out_node_is_retried_when_retries_remain(tmp_path: Path) -> None:
    # A timeout is a retryable failure kind (#6): the first Attempt sleeps past the
    # 0.2s budget and is killed (timed_out); the second, seeing the marker the
    # first left, returns immediately and succeeds. retries=1 therefore lets the
    # node ultimately succeed, with the first Attempt recorded timed_out.
    marker = tmp_path / "seen"
    command = f"if [ -e {marker} ]; then exit 0; else touch {marker}; sleep 30; fi"
    workflow = policy_shell_workflow("slow", command, timeout=0.2, retries=1)

    result = await execute_run(workflow, tmp_path / "runs")

    assert result.succeeded, "the second attempt beat the budget, so the run succeeds"
    run_dir = single_run_dir(tmp_path / "runs")
    attempts = state_rows(
        run_dir,
        "SELECT attempt, exit_status FROM attempt WHERE node_id = 'slow' ORDER BY attempt",
    )
    assert len(attempts) == 2, "the timed-out first attempt and the succeeding second are recorded"
    assert attempts[0]["exit_status"] == -1, "a timeout records exit_status -1 for the first try"


@pytest.mark.asyncio
async def test_an_errored_adapter_failure_is_not_retried(tmp_path: Path) -> None:
    # Retry policy boundary (#6): an ERRORED failure (here a mock agent Node with
    # no fixture, an AdapterError) is an Adapter/internal fault that is almost
    # always deterministic, so it is NOT retried even with retries set — retrying
    # it would only burn Attempts. Exactly one Attempt is recorded.
    raw: dict[str, Any] = {
        "name": "sample",
        "version": 1,
        "nodes": [
            {
                "id": "summarize",
                "kind": "agent",
                "retries": 3,
                "inputs": {"adapter": "mock", "prompt": "do it"},
            }
        ],
    }
    workflow = normalize_workflow(raw, source="<test>")

    result = await execute_run(workflow, tmp_path / "runs")

    assert not result.succeeded
    run_dir = single_run_dir(tmp_path / "runs")
    (node,) = state_rows(run_dir, "SELECT * FROM node")
    assert node["status"] == "errored", "an adapter fault is classified errored, not failed"
    attempts = state_rows(run_dir, "SELECT * FROM attempt WHERE node_id = 'summarize'")
    assert len(attempts) == 1, "an errored failure is not retried even with retries set"


@pytest.mark.asyncio
async def test_resume_completes_an_interrupted_run_without_rerunning_completed_nodes(
    tmp_path: Path,
) -> None:
    # Acceptance criterion #6.4: `caw resume` completes an interrupted workflow
    # without re-running completed Nodes. `build` succeeds and `test` fails on the
    # first run (its marker is absent), so `deploy` is skipped and the run fails.
    # `build` appends to a counter on every run; `test` succeeds once its marker
    # exists. On resume, the already-succeeded `build` must NOT run again (the
    # counter stays at one), `test` re-runs and now succeeds, and `deploy` runs —
    # so the resumed run succeeds, reusing the SAME run id and run directory.
    runs_root = tmp_path / "runs"
    build_count = tmp_path / "build.count"
    test_marker = tmp_path / "test.marker"
    deployed = tmp_path / "deployed"
    raw: dict[str, Any] = {
        "name": "sample",
        "version": 1,
        "nodes": [
            {"id": "build", "kind": "shell", "inputs": {"command": f"echo x >> {build_count}"}},
            {
                "id": "test",
                "kind": "shell",
                "needs": ["build"],
                "inputs": {
                    "command": (
                        f"if [ -e {test_marker} ]; then exit 0; "
                        f"else touch {test_marker}; exit 7; fi"
                    )
                },
            },
            {
                "id": "deploy",
                "kind": "shell",
                "needs": ["test"],
                "inputs": {"command": f"touch {deployed}"},
            },
        ],
    }
    workflow = normalize_workflow(raw, source="<test>")

    first = await execute_run(workflow, runs_root)
    assert not first.succeeded, "the first run fails because `test` fails"
    assert build_count.read_text(encoding="utf-8").split() == ["x"], "build ran once"
    assert not deployed.exists(), "deploy is skipped on the first run"

    resumed = await resume_run(first.run_id, runs_root)

    assert resumed.succeeded, "the resumed run completes the interrupted workflow"
    assert resumed.run_id == first.run_id, "resume reuses the same run id"
    assert build_count.read_text(encoding="utf-8").split() == ["x"], (
        "the already-succeeded build node is NOT re-run on resume"
    )
    assert deployed.exists(), "deploy runs on resume once test succeeds"

    # The same run directory is reused, and the run's final status is succeeded.
    run_dir = single_run_dir(runs_root)
    assert run_dir.name == first.run_id
    (run,) = state_rows(run_dir, "SELECT * FROM run")
    assert run["status"] == "succeeded"
    nodes = {row["node_id"]: row["status"] for row in state_rows(run_dir, "SELECT * FROM node")}
    assert nodes == {"build": "succeeded", "test": "succeeded", "deploy": "succeeded"}


@pytest.mark.asyncio
async def test_resuming_a_run_whose_blocker_fails_again_re_skips_its_dependent_cleanly(
    tmp_path: Path,
) -> None:
    # Resume idempotency for the skip path (#6 review): on the first run `build`
    # fails and its dependent `deploy` is recorded `skipped` — so a `node` row for
    # `deploy` already exists. When the run is resumed and `build` fails AGAIN, the
    # scheduler re-skips `deploy`; that re-skip must flip the EXISTING row to
    # `skipped` (an UPDATE) rather than re-INSERT it, which would breach the
    # `(run_id, node_id)` PK and crash the resume with an IntegrityError instead of
    # returning a clean failed RunResult. The blocker stays a hard failure
    # (`exit 7` every time), so the resumed run must end failed, not crash.
    runs_root = tmp_path / "runs"
    raw: dict[str, Any] = {
        "name": "sample",
        "version": 1,
        "nodes": [
            {"id": "build", "kind": "shell", "inputs": {"command": "exit 7"}},
            {
                "id": "deploy",
                "kind": "shell",
                "needs": ["build"],
                "inputs": {"command": "echo deployed"},
            },
        ],
    }
    workflow = normalize_workflow(raw, source="<test>")

    first = await execute_run(workflow, runs_root)
    assert not first.succeeded, "the first run fails because `build` fails"
    run_dir = single_run_dir(runs_root)
    first_nodes = {
        row["node_id"]: row["status"] for row in state_rows(run_dir, "SELECT * FROM node")
    }
    assert first_nodes == {"build": "failed", "deploy": "skipped"}, (
        "the first run leaves a `deploy` row already present, the precondition for the bug"
    )

    # Resuming must NOT crash with an IntegrityError when `build` fails again and
    # `deploy` is re-skipped over its pre-existing row.
    resumed = await resume_run(first.run_id, runs_root)

    assert not resumed.succeeded, "the blocker fails again, so the resumed run is still failed"
    assert resumed.run_id == first.run_id, "resume reuses the same run id"
    nodes = {row["node_id"]: row["status"] for row in state_rows(run_dir, "SELECT * FROM node")}
    assert nodes == {"build": "failed", "deploy": "skipped"}, (
        "the blocker is recorded failed again and the dependent is re-recorded skipped"
    )


@pytest.mark.asyncio
async def test_resuming_a_run_whose_blocker_now_succeeds_runs_the_previously_skipped_dependent(
    tmp_path: Path,
) -> None:
    # The happy sibling of the re-skip case (#6 review): when the blocker now
    # SUCCEEDS on resume, the previously-skipped dependent must re-run to
    # completion. `build` fails the first run (no marker), so `deploy` is skipped;
    # on resume the marker exists, `build` succeeds, and `deploy` — whose row
    # exists from the first run — flips to running via record_node_running and then
    # runs its command. The marker file proves the previously-skipped node ran.
    runs_root = tmp_path / "runs"
    build_marker = tmp_path / "build.marker"
    deployed = tmp_path / "deployed"
    raw: dict[str, Any] = {
        "name": "sample",
        "version": 1,
        "nodes": [
            {
                "id": "build",
                "kind": "shell",
                "inputs": {
                    "command": (
                        f"if [ -e {build_marker} ]; then exit 0; "
                        f"else touch {build_marker}; exit 7; fi"
                    )
                },
            },
            {
                "id": "deploy",
                "kind": "shell",
                "needs": ["build"],
                "inputs": {"command": f"touch {deployed}"},
            },
        ],
    }
    workflow = normalize_workflow(raw, source="<test>")

    first = await execute_run(workflow, runs_root)
    assert not first.succeeded, "the first run fails because `build` fails"
    assert not deployed.exists(), "deploy is skipped on the first run"
    run_dir = single_run_dir(runs_root)
    first_nodes = {
        row["node_id"]: row["status"] for row in state_rows(run_dir, "SELECT * FROM node")
    }
    assert first_nodes == {"build": "failed", "deploy": "skipped"}

    resumed = await resume_run(first.run_id, runs_root)

    assert resumed.succeeded, "the blocker now succeeds, so the resumed run completes"
    assert deployed.exists(), "the previously-skipped dependent re-runs once its blocker succeeds"
    nodes = {row["node_id"]: row["status"] for row in state_rows(run_dir, "SELECT * FROM node")}
    assert nodes == {"build": "succeeded", "deploy": "succeeded"}


@pytest.mark.asyncio
async def test_resuming_an_already_succeeded_run_is_refused(tmp_path: Path) -> None:
    # Resume eligibility (#6): a Run that already succeeded has nothing to do, so
    # resuming it is refused with a clear error rather than re-running it or
    # silently no-opping. Unknown and succeeded ids are the two refusal cases.
    runs_root = tmp_path / "runs"
    workflow = shell_workflow("echo ok")
    first = await execute_run(workflow, runs_root)
    assert first.succeeded

    with pytest.raises(ResumeError, match="not resumable"):
        await resume_run(first.run_id, runs_root)


@pytest.mark.asyncio
async def test_resuming_an_unknown_run_id_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ResumeError, match="no run directory"):
        await resume_run("no-such-run", tmp_path / "runs")


@pytest.mark.asyncio
async def test_resuming_a_run_with_a_tampered_snapshot_is_refused(tmp_path: Path) -> None:
    # Snapshot integrity on resume (#70): the run directory persists a
    # `definition_checksum` alongside the normalized workflow. If the workflow in
    # the snapshot is tampered with after the run, the checksum the kernel
    # recomputes from the reconstructed Workflow no longer matches the stored one,
    # so resume must REFUSE with a clear ResumeError rather than silently resuming
    # a corrupted definition. The first run fails (so it is resume-eligible), then
    # the snapshot's workflow is mutated WITHOUT updating the stored checksum.
    runs_root = tmp_path / "runs"
    workflow = shell_workflow("exit 7")
    first = await execute_run(workflow, runs_root)
    assert not first.succeeded, "the first run fails, so it is resume-eligible"

    run_dir = single_run_dir(runs_root)
    snapshot_path = run_dir / "workflow.normalized.json"
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    snapshot["workflow"]["name"] = "tampered"
    snapshot_path.write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")

    with pytest.raises(ResumeError, match="checksum"):
        await resume_run(first.run_id, runs_root)


class FailingCustomAdapter(Adapter):
    """A test-only injected Adapter that always fails, so its run is resume-eligible.

    Stands in for a custom/real-CLI Adapter (#9 / #11) that is supplied at run time
    via a populated registry but is NOT a built-in adapter name — the trigger for
    the AC2 resume case.
    """

    async def invoke(self, invocation: AgentInvocation) -> AgentResult:
        return AgentResult(exit_status=7, stderr="custom adapter failed")


@pytest.mark.asyncio
async def test_resuming_a_run_whose_adapter_is_absent_from_the_registry_is_refused(
    tmp_path: Path,
) -> None:
    # Custom-adapter resume (#70): a run that used an injected (non-builtin)
    # Adapter persists that adapter's NAME in its snapshot but cannot persist the
    # Adapter itself. Resuming WITHOUT supplying the same registry re-validates the
    # snapshot against the supplied registry's known adapters — which lack the
    # custom name — so the model raises a pydantic ValidationError ("unknown
    # adapter"). Resume must translate that into an actionable ResumeError naming
    # the missing adapter and hinting to supply the right registry, never leaking a
    # raw ValidationError to the caller. The first run uses a `custom` adapter and
    # fails (so it is resume-eligible); the resume omits the registry.
    runs_root = tmp_path / "runs"
    registry = AdapterRegistry({"custom": FailingCustomAdapter()})
    raw: dict[str, Any] = {
        "name": "sample",
        "version": 1,
        "nodes": [
            {
                "id": "agent",
                "kind": "agent",
                "needs": [],
                "inputs": {"adapter": "custom", "prompt": "do it"},
            }
        ],
    }
    workflow = normalize_workflow(raw, source="<test>", known_adapters=frozenset({"custom"}))
    first = await execute_run(workflow, runs_root, registry=registry)
    assert not first.succeeded, "the first run fails, so it is resume-eligible"

    # Resuming without the custom adapter's registry must surface a clean,
    # actionable ResumeError, not a raw pydantic ValidationError.
    with pytest.raises(ResumeError, match="custom"):
        await resume_run(first.run_id, runs_root)


@pytest.mark.asyncio
async def test_a_cancelled_run_is_consistent_and_resumes_to_completion(tmp_path: Path) -> None:
    # Acceptance criterion #6.3: cancelling a run mid-flight leaves State
    # consistent (the run errored, the in-flight node errored — never left
    # `running`) and the run resumable. A gate file blocks the node until the
    # resume releases it; cancelling the first run interrupts it, and resume
    # re-runs the interrupted node, which now completes because the gate is open.
    runs_root = tmp_path / "runs"
    gate = tmp_path / "gate"
    done = tmp_path / "done"
    workflow = policy_shell_workflow(
        "blocked",
        f"while [ ! -e {gate} ]; do sleep 0.05; done; touch {done}",
    )

    run_task = asyncio.create_task(execute_run(workflow, runs_root))
    run_dir = None
    for _ in range(200):
        if runs_root.exists() and list(runs_root.iterdir()):
            run_dir = single_run_dir(runs_root)
            nodes = state_rows(run_dir, "SELECT status FROM node")
            if nodes and nodes[0]["status"] == "running":
                break
        await asyncio.sleep(0.05)
    else:
        run_task.cancel()
        pytest.fail("the node never reached running before cancellation")

    run_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_task

    # State is consistent after cancellation: the run and its in-flight node are
    # errored, not left running — the precondition for a clean resume.
    assert run_dir is not None
    (run,) = state_rows(run_dir, "SELECT * FROM run")
    assert run["status"] == "errored"
    (node,) = state_rows(run_dir, "SELECT * FROM node")
    assert node["status"] == "errored"
    assert not done.exists(), "the node was interrupted before completing"

    # Open the gate and resume: the interrupted node re-runs and completes.
    gate.touch()
    resumed = await resume_run(run["run_id"], runs_root)

    assert resumed.succeeded, "the cancelled run resumes to completion"
    assert done.exists(), "the interrupted node ran again and finished on resume"
    (node,) = state_rows(run_dir, "SELECT * FROM node")
    assert node["status"] == "succeeded"


@pytest.mark.asyncio
async def test_run_executes_nodes_in_dependency_order_not_declaration_order(
    tmp_path: Path,
) -> None:
    log = tmp_path / "order.log"
    workflow = shell_workflow(
        ("second", f"echo second >> {log}", ["first"]),
        ("first", f"echo first >> {log}", []),
    )

    result = await execute_run(workflow, tmp_path / "runs")

    assert result.succeeded
    assert log.read_text(encoding="utf-8").split() == ["first", "second"]


@pytest.mark.asyncio
async def test_a_join_node_runs_only_after_both_of_its_branches_complete(
    tmp_path: Path,
) -> None:
    # The durable contract this seam owns: a join runs after every branch it
    # needs has completed. It deliberately does NOT assert a strict total
    # completion order between the independent branches — that is the order
    # function's declaration-order tie-break (pinned in tests/test_model.py),
    # and parallel scheduling (#4) may legitimately interleave the branches.
    log = tmp_path / "order.log"
    workflow = shell_workflow(
        ("join", f"echo join >> {log}", ["left", "right"]),
        ("left", f"echo left >> {log}", []),
        ("right", f"echo right >> {log}", []),
    )

    result = await execute_run(workflow, tmp_path / "runs")

    assert result.succeeded
    completions = log.read_text(encoding="utf-8").split()
    assert set(completions) == {"left", "right", "join"}
    assert completions.index("join") > completions.index("left")
    assert completions.index("join") > completions.index("right")


def barrier_command(own_marker: Path, barrier_dir: Path, party: int, timeout_s: int = 30) -> str:
    """A shell command that touches its own marker, then blocks until ``party`` exist.

    This is a filesystem barrier: a branch only gets past it once every branch
    has reached it, so all branches reaching the barrier proves they ran
    genuinely concurrently — no wall-clock sleep is asserted. If fewer than
    ``party`` branches can run at once (e.g. concurrency 1), a waiting branch
    never sees the full party and exits non-zero at the timeout failsafe, which
    is what makes a serialized run observably fail instead of hang forever.
    """
    return (
        f"touch {own_marker}; "
        f"for _ in $(seq 1 {timeout_s * 10}); do "
        f"  count=$(ls {barrier_dir} | wc -l); "
        f'  if [ "$count" -ge {party} ]; then exit 0; fi; '
        f"  sleep 0.1; "
        f"done; "
        f"exit 1"
    )


@pytest.mark.asyncio
async def test_three_branches_run_concurrently_and_the_join_runs_after_all(tmp_path: Path) -> None:
    # The acceptance criterion (#4): a three-branch parallel workflow completes
    # with all branches AND the join executed, and the branches genuinely
    # overlap. The filesystem barrier is the deterministic proof: each branch
    # only finishes once all three have reached the barrier, so the run can only
    # succeed if the three ran at once. The default concurrency (4) permits it.
    barrier_dir = tmp_path / "barrier"
    barrier_dir.mkdir()
    log = tmp_path / "join.log"
    workflow = shell_workflow(
        ("a", barrier_command(barrier_dir / "a", barrier_dir, party=3), []),
        ("b", barrier_command(barrier_dir / "b", barrier_dir, party=3), []),
        ("c", barrier_command(barrier_dir / "c", barrier_dir, party=3), []),
        ("join", f"echo join >> {log}", ["a", "b", "c"]),
    )

    result = await execute_run(workflow, tmp_path / "runs")

    assert result.succeeded, "all three branches reached the barrier, so the run succeeded"
    attempted = {node_result.node_id for node_result in result.node_results}
    assert attempted == {"a", "b", "c", "join"}, "every branch and the join executed"
    assert log.read_text(encoding="utf-8").split() == ["join"], "the join ran after all branches"


@pytest.mark.asyncio
async def test_concurrency_limit_one_serializes_branches_so_the_barrier_cannot_be_met(
    tmp_path: Path,
) -> None:
    # The teeth of the concurrency proof (#4): with the limit pinned to 1 the
    # scheduler runs one branch at a time, so the first branch to reach the
    # three-party barrier can never see its siblings — they cannot start until
    # it finishes — and exits non-zero at its (short) timeout. A run that
    # genuinely honors the limit therefore fails here, while an unbounded
    # scheduler would pass. This is what makes the concurrent run a real proof.
    barrier_dir = tmp_path / "barrier"
    barrier_dir.mkdir()
    raw = {
        "name": "sample",
        "version": 1,
        "concurrency": 1,
        "nodes": [
            {
                "id": node_id,
                "kind": "shell",
                "needs": [],
                "inputs": {
                    "command": barrier_command(barrier_dir / node_id, barrier_dir, 3, timeout_s=2)
                },
            }
            for node_id in ("a", "b", "c")
        ],
    }
    workflow = normalize_workflow(raw, source="<test>")

    result = await execute_run(workflow, tmp_path / "runs")

    assert not result.succeeded, "a serialized run cannot meet a three-party barrier"


@pytest.mark.asyncio
async def test_concurrency_limit_caps_simultaneous_attempts(tmp_path: Path) -> None:
    # The limit is an upper bound on simultaneity, observed without sleeps: each
    # node records the peak number of concurrent peers it saw via a shared
    # counter file. With four independent nodes and a limit of 2, the peak must
    # never exceed 2. (Each node bumps a live-count file on entry and decrements
    # on exit; the recorded maximum is the observed concurrency.)
    live = tmp_path / "live"
    peak = tmp_path / "peak"
    live.write_text("0", encoding="utf-8")
    peak.write_text("0", encoding="utf-8")
    # A small Python helper keeps the read-modify-write of the counter atomic
    # enough for the assertion: the event loop is single-threaded, but the
    # subprocesses are not, so each uses an O_EXCL lock directory as a mutex.
    bump = (
        f"lock={tmp_path}/lock; "
        f"while ! mkdir $lock 2>/dev/null; do sleep 0.01; done; "
        f"n=$(cat {live}); n=$((n+1)); echo $n > {live}; "
        f"p=$(cat {peak}); if [ $n -gt $p ]; then echo $n > {peak}; fi; "
        f"rmdir $lock; "
        f"sleep 0.2; "
        f"while ! mkdir $lock 2>/dev/null; do sleep 0.01; done; "
        f"n=$(cat {live}); n=$((n-1)); echo $n > {live}; rmdir $lock"
    )
    raw = {
        "name": "sample",
        "version": 1,
        "concurrency": 2,
        "nodes": [
            {"id": node_id, "kind": "shell", "needs": [], "inputs": {"command": bump}}
            for node_id in ("a", "b", "c", "d")
        ],
    }
    workflow = normalize_workflow(raw, source="<test>")

    result = await execute_run(workflow, tmp_path / "runs")

    assert result.succeeded
    assert int(peak.read_text(encoding="utf-8")) <= 2, (
        "no more than `concurrency` nodes run at once"
    )


@pytest.mark.asyncio
async def test_transitive_dependents_of_a_failed_node_never_run(tmp_path: Path) -> None:
    # Branch-failure isolation (#4): a node failure prevents only its transitive
    # dependents from being attempted. Here `deploy` needs `build`, so a failing
    # `build` keeps `deploy`'s command from ever running.
    marker = tmp_path / "deployed.txt"
    workflow = shell_workflow(
        ("build", "exit 7", []),
        ("deploy", f"touch {marker}", ["build"]),
    )

    result = await execute_run(workflow, tmp_path / "runs")

    assert not result.succeeded
    assert not marker.exists(), "a dependent of a failed node never runs"
    attempted = {node_result.node_id for node_result in result.node_results}
    assert attempted == {"build"}, "only the failed node was attempted; its dependent was not"


@pytest.mark.asyncio
async def test_a_failing_branch_prevents_the_join_and_marks_the_run_failed(
    tmp_path: Path,
) -> None:
    # The acceptance criterion (#4): in a fan-in, a failing branch prevents the
    # join from running and marks the run failed. `left` succeeds, `right`
    # fails, and the join needs both — so the join is skipped, never executed,
    # and the run as a whole fails.
    joined = tmp_path / "joined.txt"
    workflow = shell_workflow(
        ("left", "echo left", []),
        ("right", "exit 7", []),
        ("join", f"touch {joined}", ["left", "right"]),
    )

    result = await execute_run(workflow, tmp_path / "runs")

    assert not result.succeeded, "a failing branch fails the run"
    assert not joined.exists(), "the join never runs when a branch it needs fails"
    attempted = {node_result.node_id for node_result in result.node_results}
    assert attempted == {"left", "right"}, "both branches were attempted; the join was not"
    assert result.skipped_node_ids == ("join",), "the join is recorded skipped"


@pytest.mark.asyncio
async def test_a_failure_skips_the_whole_transitive_chain_of_dependents(tmp_path: Path) -> None:
    # Skipping is transitive: fail -> mid -> leaf. A failed `fail` skips `mid`,
    # and because `mid` never succeeds, `leaf` is skipped too — the skip walks
    # the full dependent chain, not just the immediate neighbour.
    workflow = shell_workflow(
        ("fail", "exit 7", []),
        ("mid", "echo mid", ["fail"]),
        ("leaf", "echo leaf", ["mid"]),
    )

    result = await execute_run(workflow, tmp_path / "runs")

    assert not result.succeeded
    attempted = {node_result.node_id for node_result in result.node_results}
    assert attempted == {"fail"}, "neither the dependent nor its dependent ran"
    assert set(result.skipped_node_ids) == {"mid", "leaf"}


@pytest.mark.asyncio
async def test_a_join_is_skipped_when_only_one_of_its_branches_fails(tmp_path: Path) -> None:
    # A diamond join needs both a failing and a succeeding branch. The join is a
    # transitive dependent of the failure, so it is skipped even though its other
    # branch succeeded — and the scheduler still terminates cleanly rather than
    # waiting forever for the never-satisfiable join.
    joined = tmp_path / "joined.txt"
    workflow = shell_workflow(
        ("ok", "echo ok", []),
        ("fail", "exit 7", []),
        ("join", f"touch {joined}", ["ok", "fail"]),
    )

    result = await execute_run(workflow, tmp_path / "runs")

    assert not result.succeeded
    assert not joined.exists(), "a join with any failed branch never runs"
    attempted = {node_result.node_id for node_result in result.node_results}
    assert attempted == {"ok", "fail"}, "the succeeding branch ran; the join did not"
    assert result.skipped_node_ids == ("join",)


@pytest.mark.asyncio
async def test_an_independent_branch_still_runs_when_another_branch_fails(tmp_path: Path) -> None:
    # Branch-failure isolation (#4): `failing` and `independent` share no
    # dependency edge, so a failure in one must not prevent the other from
    # running. This is the behavior the old positional stop-the-run got wrong.
    marker = tmp_path / "independent.txt"
    workflow = shell_workflow(
        ("failing", "exit 7", []),
        ("independent", f"touch {marker}", []),
    )

    result = await execute_run(workflow, tmp_path / "runs")

    assert not result.succeeded, "the run is failed because one branch failed"
    assert marker.exists(), "an independent branch runs even though another branch failed"
    attempted = {node_result.node_id for node_result in result.node_results}
    assert attempted == {"failing", "independent"}


@pytest.mark.asyncio
async def test_a_node_whose_when_predicate_is_false_is_skipped_and_never_executed(
    tmp_path: Path,
) -> None:
    # Acceptance criterion (#7): a node with a false `when` is marked `skipped`
    # and never executed. `classify` emits "billing"; `act` only runs when the
    # label equals "shipping", so its predicate is false — its marker command
    # must never run, and it is recorded `skipped` with cause `when_false` and no
    # blocker (it was not blocked by a failure; its own gate closed).
    marker = tmp_path / "acted.txt"
    workflow = conditional_workflow(
        shell("classify", "echo billing"),
        shell(
            "act",
            f"touch {marker}",
            needs=["classify"],
            when={
                "ref": {"node": "classify", "field": "stdout"},
                "op": "equals",
                "value": "shipping",
            },
        ),
    )

    result = await execute_run(workflow, tmp_path / "runs")

    assert result.succeeded, "a skipped-by-when node does not fail the run"
    assert not marker.exists(), "a node whose `when` is false never executes its command"
    attempted = {node_result.node_id for node_result in result.node_results}
    assert attempted == {"classify"}, "only the gate ran; the gated node was skipped"
    assert result.skipped_node_ids == ("act",)
    assert result.skipped_blockers.get("act") is None, "a when-skip has no failure blocker"
    run_dir = single_run_dir(tmp_path / "runs")
    rows = {row["node_id"]: row for row in state_rows(run_dir, "SELECT * FROM node")}
    assert rows["act"]["status"] == "skipped"
    assert rows["act"]["cause"] == "when_false", "the skip cause distinguishes a closed gate"


@pytest.mark.asyncio
async def test_an_equals_leaf_does_not_coerce_a_bool_value_to_an_int_exit_status(
    tmp_path: Path,
) -> None:
    # FIX 3 (#74): Python evaluates `0 == False` as True, so an `equals` leaf must
    # NOT coerce bool to int. `probe` exits 0 (succeeds). `match_int` gates on
    # exit_status == 0 — a genuine int match, so it RUNS — while `match_bool` gates
    # on exit_status == false, which must be FALSE (bool vs int mismatch) so it is
    # SKIPPED. Without the guard `0 == False` would wrongly open `match_bool`'s
    # gate.
    int_marker = tmp_path / "int.txt"
    bool_marker = tmp_path / "bool.txt"
    workflow = conditional_workflow(
        shell("probe", "true"),
        shell(
            "match_int",
            f"touch {int_marker}",
            needs=["probe"],
            when={"ref": {"node": "probe", "field": "exit_status"}, "op": "equals", "value": 0},
        ),
        shell(
            "match_bool",
            f"touch {bool_marker}",
            needs=["probe"],
            when={
                "ref": {"node": "probe", "field": "exit_status"},
                "op": "equals",
                "value": False,
            },
        ),
    )

    result = await execute_run(workflow, tmp_path / "runs")

    assert result.succeeded
    assert int_marker.exists(), "exit_status == 0 is a real int match, so the gate opens"
    assert not bool_marker.exists(), "exit_status == false must not match via 0 == False coercion"
    assert result.skipped_causes.get("match_bool") == "when_false"


@pytest.mark.asyncio
async def test_an_equals_on_stdout_leaf_matches_a_node_that_echoes_the_value(
    tmp_path: Path,
) -> None:
    # FIX 2 (#74): `echo X` yields stdout "X\n", stored verbatim, so an
    # `equals`-on-stdout leaf must tolerate the trailing newline or it could never
    # match an `echo`. `classify` runs `echo billing`; `act` gates on
    # stdout == "billing" — which must be TRUE despite the trailing newline — so
    # `act` runs. Before the fix the leaf compared "billing\n" == "billing" and
    # was always false.
    marker = tmp_path / "acted.txt"
    workflow = conditional_workflow(
        shell("classify", "echo billing"),
        shell(
            "act",
            f"touch {marker}",
            needs=["classify"],
            when={
                "ref": {"node": "classify", "field": "stdout"},
                "op": "equals",
                "value": "billing",
            },
        ),
    )

    result = await execute_run(workflow, tmp_path / "runs")

    assert result.succeeded
    assert marker.exists(), "`equals` on stdout matches an echo despite the trailing newline"
    assert result.skipped_node_ids == (), "the gate is open, so nothing is skipped"


@pytest.mark.asyncio
async def test_a_node_whose_when_predicate_is_true_runs_normally(tmp_path: Path) -> None:
    # The complement of the false-gate case (#7): a node whose `when` holds runs
    # like any other node. `classify` emits "shipping"; `act` runs iff the label
    # CONTAINS "shipping" (a substring test that tolerates echo's trailing
    # newline), so its gate is open — its marker command runs and it is recorded
    # `succeeded`, never skipped.
    marker = tmp_path / "acted.txt"
    workflow = conditional_workflow(
        shell("classify", "echo shipping"),
        shell(
            "act",
            f"touch {marker}",
            needs=["classify"],
            when={
                "ref": {"node": "classify", "field": "stdout"},
                "op": "contains",
                "value": "shipping",
            },
        ),
    )

    result = await execute_run(workflow, tmp_path / "runs")

    assert result.succeeded
    assert marker.exists(), "a node whose `when` holds runs its command"
    attempted = {node_result.node_id for node_result in result.node_results}
    assert attempted == {"classify", "act"}, "the open gate let the gated node run"
    assert result.skipped_node_ids == (), "nothing is skipped when the gate is open"


@pytest.mark.asyncio
async def test_an_all_of_when_predicate_drives_a_skip_in_the_scheduler(tmp_path: Path) -> None:
    # Composition works in the scheduler, not only in the model (#7): an `all_of`
    # is true iff EVERY child is. `flag` emits "go" and `mode` emits "fast"; `act`
    # requires both flag=="go" AND mode=="slow", so the conjunction is FALSE and
    # the gate closes — `act` is skipped `when_false` and never runs.
    marker = tmp_path / "acted.txt"
    workflow = conditional_workflow(
        shell("flag", "printf go"),
        shell("mode", "printf fast"),
        shell(
            "act",
            f"touch {marker}",
            needs=["flag", "mode"],
            when={
                "all_of": [
                    {"ref": {"node": "flag", "field": "stdout"}, "op": "equals", "value": "go"},
                    {"ref": {"node": "mode", "field": "stdout"}, "op": "equals", "value": "slow"},
                ]
            },
        ),
    )

    result = await execute_run(workflow, tmp_path / "runs")

    assert result.succeeded, "a closed `all_of` gate does not fail the run"
    assert not marker.exists(), "the conjunction is false, so the gated node never runs"
    assert result.skipped_node_ids == ("act",)
    assert result.skipped_causes.get("act") == "when_false"


@pytest.mark.asyncio
async def test_an_any_of_when_predicate_drives_a_run_in_the_scheduler(tmp_path: Path) -> None:
    # The `any_of` combinator end-to-end (#7): true iff ANY child is. `flag` emits
    # "go" and `mode` emits "fast"; `act` runs if flag=="stop" OR mode=="fast", so
    # the disjunction is TRUE via its second child — the gate opens and `act` runs.
    marker = tmp_path / "acted.txt"
    workflow = conditional_workflow(
        shell("flag", "printf go"),
        shell("mode", "printf fast"),
        shell(
            "act",
            f"touch {marker}",
            needs=["flag", "mode"],
            when={
                "any_of": [
                    {"ref": {"node": "flag", "field": "stdout"}, "op": "equals", "value": "stop"},
                    {"ref": {"node": "mode", "field": "stdout"}, "op": "equals", "value": "fast"},
                ]
            },
        ),
    )

    result = await execute_run(workflow, tmp_path / "runs")

    assert result.succeeded
    assert marker.exists(), "the disjunction holds via one child, so the gated node runs"
    assert result.skipped_node_ids == (), "an open `any_of` gate skips nothing"


@pytest.mark.asyncio
async def test_a_not_when_predicate_drives_a_skip_in_the_scheduler(tmp_path: Path) -> None:
    # The `not` combinator end-to-end (#7): negates its single child. `flag` emits
    # "go"; `act` runs only if NOT (flag=="go"), so the negation is FALSE and the
    # gate closes — `act` is skipped `when_false`.
    marker = tmp_path / "acted.txt"
    workflow = conditional_workflow(
        shell("flag", "printf go"),
        shell(
            "act",
            f"touch {marker}",
            needs=["flag"],
            when={
                "not": {"ref": {"node": "flag", "field": "stdout"}, "op": "equals", "value": "go"}
            },
        ),
    )

    result = await execute_run(workflow, tmp_path / "runs")

    assert result.succeeded
    assert not marker.exists(), "the negation is false, so the gated node never runs"
    assert result.skipped_node_ids == ("act",)
    assert result.skipped_causes.get("act") == "when_false"


@pytest.mark.asyncio
async def test_a_dependent_of_a_when_skipped_node_is_skipped_blocked_along_the_whole_chain(
    tmp_path: Path,
) -> None:
    # A when-skip propagates like a failure-skip down a default (`join: all`)
    # chain (#7): `classify` emits "billing"; `act` only runs on "shipping" so it
    # is skipped `when_false`; `notify` needs `act` and `audit` needs `notify`, so
    # the whole transitive chain is skipped `blocked`. The IMMEDIATE dependent's
    # blocker is the when-skipped node that orphaned it (`act`), and the skip walks
    # all the way to the leaf — not just the immediate neighbour.
    acted = tmp_path / "acted.txt"
    notified = tmp_path / "notified.txt"
    audited = tmp_path / "audited.txt"
    workflow = conditional_workflow(
        shell("classify", "echo billing"),
        shell(
            "act",
            f"touch {acted}",
            needs=["classify"],
            when={
                "ref": {"node": "classify", "field": "stdout"},
                "op": "equals",
                "value": "shipping",
            },
        ),
        shell("notify", f"touch {notified}", needs=["act"]),
        shell("audit", f"touch {audited}", needs=["notify"]),
    )

    result = await execute_run(workflow, tmp_path / "runs")

    assert result.succeeded, "a benign when-skip and its blocked chain do not fail the run"
    assert not acted.exists() and not notified.exists() and not audited.exists()
    attempted = {node_result.node_id for node_result in result.node_results}
    assert attempted == {"classify"}, "only the gate ran; the whole chain was skipped"
    assert set(result.skipped_node_ids) == {"act", "notify", "audit"}
    assert result.skipped_causes["act"] == "when_false", "the gate node's own skip is when_false"
    assert result.skipped_causes["notify"] == "blocked"
    assert result.skipped_causes["audit"] == "blocked"
    assert result.skipped_blockers["notify"] == "act", (
        "the immediate dependent is blocked by the when-skipped node that orphaned it"
    )
    assert result.skipped_blockers["audit"] == "notify", (
        "the transitive dependent is blocked by the skip that orphaned it"
    )


@pytest.mark.asyncio
async def test_a_join_any_node_runs_when_one_branch_skipped_and_one_succeeded(
    tmp_path: Path,
) -> None:
    # Acceptance criterion #7.3, the classify-and-act demo: `classify` emits a
    # label; two mutually-exclusive action branches each gate on it — `ship` runs
    # only on "shipping" (its gate closes here) and `bill` runs when the label
    # CONTAINS "billing" (its gate opens here). `summary` is a `join: any` over
    # both, so it tolerates the skipped `ship` branch and runs on the surviving
    # succeeded `bill` branch — exactly one action fired, and the join still ran.
    billed = tmp_path / "billed.txt"
    shipped = tmp_path / "shipped.txt"
    summarized = tmp_path / "summarized.txt"
    workflow = conditional_workflow(
        shell("classify", "echo billing"),
        shell(
            "ship",
            f"touch {shipped}",
            needs=["classify"],
            when={
                "ref": {"node": "classify", "field": "stdout"},
                "op": "equals",
                "value": "shipping",
            },
        ),
        shell(
            "bill",
            f"touch {billed}",
            needs=["classify"],
            when={
                "ref": {"node": "classify", "field": "stdout"},
                "op": "contains",
                "value": "billing",
            },
        ),
        shell("summary", f"touch {summarized}", needs=["ship", "bill"], join="any"),
    )

    result = await execute_run(workflow, tmp_path / "runs")

    assert result.succeeded
    assert billed.exists(), "the matching action branch ran"
    assert not shipped.exists(), "the non-matching action branch was gated out"
    assert summarized.exists(), "a `join: any` runs when at least one branch succeeded"
    attempted = {node_result.node_id for node_result in result.node_results}
    assert attempted == {"classify", "bill", "summary"}, "the tolerant join ran past the skip"
    assert result.skipped_node_ids == ("ship",), "only the gated-out branch was skipped"
    assert result.skipped_causes["ship"] == "when_false"


@pytest.mark.asyncio
async def test_a_join_any_node_is_skipped_when_all_of_its_branches_skipped(
    tmp_path: Path,
) -> None:
    # The tolerant-join floor (#7): a `join: any` runs iff at least one branch
    # executed, so when EVERY branch is gated out there is nothing to join and the
    # join is itself skipped — with cause `all_branches_skipped`, distinct from a
    # failure-blocked skip. `classify` emits "billing"; both action branches gate
    # on labels that do not match, so both skip and `summary` skips too.
    summarized = tmp_path / "summarized.txt"
    workflow = conditional_workflow(
        shell("classify", "echo billing"),
        shell(
            "ship",
            "echo ship",
            needs=["classify"],
            when={
                "ref": {"node": "classify", "field": "stdout"},
                "op": "equals",
                "value": "shipping",
            },
        ),
        shell(
            "refund",
            "echo refund",
            needs=["classify"],
            when={
                "ref": {"node": "classify", "field": "stdout"},
                "op": "equals",
                "value": "refunding",
            },
        ),
        shell("summary", f"touch {summarized}", needs=["ship", "refund"], join="any"),
    )

    result = await execute_run(workflow, tmp_path / "runs")

    assert result.succeeded, "a fully-skipped tolerant join does not fail the run"
    assert not summarized.exists(), "a `join: any` with no surviving branch never runs"
    attempted = {node_result.node_id for node_result in result.node_results}
    assert attempted == {"classify"}, "every action branch was gated out, so only classify ran"
    assert set(result.skipped_node_ids) == {"ship", "refund", "summary"}
    assert result.skipped_causes["summary"] == "all_branches_skipped", (
        "a tolerant join whose every branch skipped carries the distinct cause"
    )
    assert result.skipped_blockers.get("summary") is None, (
        "an all-branches-skipped join has no failure blocker"
    )


@pytest.mark.asyncio
async def test_a_default_join_all_node_is_skipped_when_any_branch_skipped(
    tmp_path: Path,
) -> None:
    # Acceptance criterion #7.2, the default guard: with the default `join: all`,
    # ANY skipped dependency skips the join — even though its other branch
    # succeeded. `classify` emits "billing"; `ship` is gated out (skipped
    # `when_false`) while `bill` succeeds; `summary` defaults to `join: all`, so
    # the skipped `ship` blocks it. Contrast with the `join: any` case, where
    # `summary` would have run on `bill` alone.
    billed = tmp_path / "billed.txt"
    summarized = tmp_path / "summarized.txt"
    workflow = conditional_workflow(
        shell("classify", "echo billing"),
        shell(
            "ship",
            "echo ship",
            needs=["classify"],
            when={
                "ref": {"node": "classify", "field": "stdout"},
                "op": "equals",
                "value": "shipping",
            },
        ),
        shell(
            "bill",
            f"touch {billed}",
            needs=["classify"],
            when={
                "ref": {"node": "classify", "field": "stdout"},
                "op": "contains",
                "value": "billing",
            },
        ),
        shell("summary", f"touch {summarized}", needs=["ship", "bill"]),
    )

    result = await execute_run(workflow, tmp_path / "runs")

    assert result.succeeded, "a default-join skip is benign and does not fail the run"
    assert billed.exists(), "the surviving branch still ran"
    assert not summarized.exists(), "a default `join: all` is skipped by any skipped branch"
    attempted = {node_result.node_id for node_result in result.node_results}
    assert attempted == {"classify", "bill"}, "the default join was skipped, not run"
    assert set(result.skipped_node_ids) == {"ship", "summary"}
    assert result.skipped_causes["summary"] == "blocked", (
        "a default-join skip is `blocked` by the skipped branch, not all_branches_skipped"
    )
    assert result.skipped_blockers["summary"] == "ship", (
        "the default join names the skipped branch that orphaned it as its blocker"
    )


@pytest.mark.asyncio
async def test_a_join_any_node_is_still_skipped_when_a_branch_fails(tmp_path: Path) -> None:
    # Acceptance criterion #7.4, the discriminating invariant: a FAILED dependency
    # blocks dependents REGARDLESS of join policy. `join: any` tolerates SKIPS,
    # never FAILURES. Here `ok` succeeds and `boom` FAILS (a hard non-zero exit,
    # not a benign when-skip); `summary` is `join: any` — yet it must be SKIPPED
    # and `blocked` by the failure, never run on the surviving `ok` branch. If the
    # join ran here, join would be tolerating a failure, which it must never do,
    # and the run would also have to fail. The run fails because `boom` failed.
    summarized = tmp_path / "summarized.txt"
    workflow = conditional_workflow(
        shell("ok", "echo ok"),
        shell("boom", "exit 7"),
        shell("summary", f"touch {summarized}", needs=["ok", "boom"], join="any"),
    )

    result = await execute_run(workflow, tmp_path / "runs")

    assert not result.succeeded, "a failed branch fails the run, tolerant join or not"
    assert not summarized.exists(), "a `join: any` never runs over a FAILED branch"
    attempted = {node_result.node_id for node_result in result.node_results}
    assert attempted == {"ok", "boom"}, "the join was skipped, not run, despite join: any"
    assert result.skipped_node_ids == ("summary",)
    assert result.skipped_causes["summary"] == "blocked", (
        "a failure-driven skip is `blocked`, never tolerated as all_branches_skipped"
    )
    assert result.skipped_blockers["summary"] == "boom", (
        "the tolerant join is blocked by the FAILED node, not by a skip"
    )


@pytest.mark.asyncio
async def test_a_when_leaf_referencing_a_skipped_upstream_evaluates_false_without_crashing(
    tmp_path: Path,
) -> None:
    # FIX 1 (#74): a `when` leaf that references an upstream Node which was SKIPPED
    # (so it produced no output) must evaluate FALSE, not crash the Run. `classify`
    # emits "billing"; `ship` gates on "shipping" so it skips, producing no output.
    # `summary` is a `join: any` over `ship` and `classify`, so it tolerates the
    # skipped `ship` branch and runs on the surviving `classify` branch — but its
    # OWN `when` is an `any_of` whose first clause references the SKIPPED `ship`
    # (resolves false, no output) and whose second references `classify` (true), so
    # the disjunction holds and `summary` runs. Before the fix `_output_of`
    # asserted on the skipped `ship`'s missing output and errored the whole Run.
    summarized = tmp_path / "summarized.txt"
    workflow = conditional_workflow(
        shell("classify", "echo billing"),
        shell(
            "ship",
            "echo ship",
            needs=["classify"],
            when={
                "ref": {"node": "classify", "field": "stdout"},
                "op": "equals",
                "value": "shipping",
            },
        ),
        shell(
            "summary",
            f"touch {summarized}",
            needs=["ship", "classify"],
            join="any",
            when={
                "any_of": [
                    {"ref": {"node": "ship", "field": "stdout"}, "op": "equals", "value": "ship"},
                    {
                        "ref": {"node": "classify", "field": "stdout"},
                        "op": "contains",
                        "value": "billing",
                    },
                ]
            },
        ),
    )

    result = await execute_run(workflow, tmp_path / "runs")

    assert result.succeeded, "a leaf over a skipped upstream is false, not a Run-errored crash"
    assert summarized.exists(), "the surviving `classify` clause kept the `any_of` true"
    attempted = {node_result.node_id for node_result in result.node_results}
    assert attempted == {"classify", "summary"}, "the tolerant join ran past the skipped branch"
    assert result.skipped_node_ids == ("ship",), "only the gated-out branch was skipped"


@pytest.mark.asyncio
async def test_a_when_equals_leaf_referencing_a_skipped_upstream_is_false_and_skips_the_node(
    tmp_path: Path,
) -> None:
    # FIX 1 (#74), the leaf-false floor: when EVERY clause of a node's `when`
    # resolves false — here a single `equals` leaf referencing a SKIPPED upstream —
    # the node's predicate is false and the node is skipped `when_false`, not
    # crashed. `classify` emits "billing"; `ship` skips (gate on "shipping");
    # `summary` is a `join: any` whose `when` is just an `equals` leaf over the
    # skipped `ship`, which is false, so `summary` is gated out cleanly.
    summarized = tmp_path / "summarized.txt"
    workflow = conditional_workflow(
        shell("classify", "echo billing"),
        shell(
            "ship",
            "echo ship",
            needs=["classify"],
            when={
                "ref": {"node": "classify", "field": "stdout"},
                "op": "equals",
                "value": "shipping",
            },
        ),
        shell(
            "summary",
            f"touch {summarized}",
            needs=["ship", "classify"],
            join="any",
            when={"ref": {"node": "ship", "field": "stdout"}, "op": "equals", "value": "ship"},
        ),
    )

    result = await execute_run(workflow, tmp_path / "runs")

    assert result.succeeded, "a false leaf over a skipped upstream closes the gate, not crashes"
    assert not summarized.exists(), "the gate is false, so the tolerant join never runs"
    assert result.skipped_causes["summary"] == "when_false", (
        "a node whose every clause is false (skipped-upstream leaf) is skipped when_false"
    )


@pytest.mark.asyncio
async def test_a_when_contains_leaf_referencing_a_skipped_upstream_is_false_not_a_none_substring(
    tmp_path: Path,
) -> None:
    # FIX 1 (#74), the `contains` guard: a `contains` leaf over a SKIPPED upstream
    # must be FALSE, never fall through to `str(value) in str(None)` (which would
    # wrongly match a substring of the literal text "None"). `classify` emits
    # "billing"; `ship` skips; `summary` (join: any) gates on ship.stdout CONTAINS
    # "n" — which would be TRUE against "None" but must be FALSE against a skipped
    # branch's absent output. So `summary` is skipped when_false.
    summarized = tmp_path / "summarized.txt"
    workflow = conditional_workflow(
        shell("classify", "echo billing"),
        shell(
            "ship",
            "echo ship",
            needs=["classify"],
            when={
                "ref": {"node": "classify", "field": "stdout"},
                "op": "equals",
                "value": "shipping",
            },
        ),
        shell(
            "summary",
            f"touch {summarized}",
            needs=["ship", "classify"],
            join="any",
            when={"ref": {"node": "ship", "field": "stdout"}, "op": "contains", "value": "n"},
        ),
    )

    result = await execute_run(workflow, tmp_path / "runs")

    assert result.succeeded
    assert not summarized.exists(), (
        "`contains` over a skipped upstream is false, not a substring of the text 'None'"
    )
    assert result.skipped_causes["summary"] == "when_false"


@pytest.mark.asyncio
async def test_resume_evaluates_a_when_predicate_from_persisted_state_output(
    tmp_path: Path,
) -> None:
    # A conditional workflow resumes and its `when` evaluates from persisted State,
    # not an in-memory result (#7): on resume a SUCCEEDED dependency is seeded
    # `satisfied` with no in-memory NodeResult, so the gate must read that
    # dependency's output back from State (`_output_of`'s State fallback over
    # `node_output`). `classify` succeeds emitting "billing" on the first run;
    # `prep` fails the first run (marker absent), so `act` — which needs both
    # `prep` and `classify` — is skipped `blocked`. On resume `classify` is NOT
    # re-run; `prep` now succeeds; `act`'s gate reads `classify`'s persisted output,
    # the label CONTAINS "billing", so the gate opens and `act` finally runs.
    runs_root = tmp_path / "runs"
    classify_count = tmp_path / "classify.count"
    prep_marker = tmp_path / "prep.marker"
    acted = tmp_path / "acted.txt"
    raw: dict[str, Any] = {
        "name": "sample",
        "version": 1,
        "nodes": [
            {
                "id": "classify",
                "kind": "shell",
                "inputs": {"command": f"echo billing | tee -a {classify_count}"},
            },
            {
                "id": "prep",
                "kind": "shell",
                "needs": ["classify"],
                "inputs": {
                    "command": (
                        f"if [ -e {prep_marker} ]; then exit 0; "
                        f"else touch {prep_marker}; exit 7; fi"
                    )
                },
            },
            {
                "id": "act",
                "kind": "shell",
                "needs": ["prep", "classify"],
                "inputs": {"command": f"touch {acted}"},
                "when": {
                    "ref": {"node": "classify", "field": "stdout"},
                    "op": "contains",
                    "value": "billing",
                },
            },
        ],
    }
    workflow = normalize_workflow(raw, source="<test>")

    first = await execute_run(workflow, runs_root)
    assert not first.succeeded, "the first run fails because `prep` fails"
    assert not acted.exists(), "act is skipped (blocked by prep) on the first run"
    assert first.skipped_causes["act"] == "blocked"
    assert classify_count.read_text(encoding="utf-8").count("billing") == 1, "classify ran once"

    resumed = await resume_run(first.run_id, runs_root)

    assert resumed.succeeded, "the resumed run completes once prep succeeds and the gate opens"
    assert acted.exists(), "act runs on resume: its gate read classify's output from State"
    assert classify_count.read_text(encoding="utf-8").count("billing") == 1, (
        "the already-succeeded classify is NOT re-run on resume — its output came from State"
    )
    run_dir = single_run_dir(runs_root)
    nodes = {row["node_id"]: row["status"] for row in state_rows(run_dir, "SELECT * FROM node")}
    assert nodes == {"classify": "succeeded", "prep": "succeeded", "act": "succeeded"}


def test_stdin_reading_node_completes_instead_of_hanging_the_run(
    write_workflow: Callable[[str], Path], tmp_path: Path
) -> None:
    # A real caw process is required: pytest redirects the test process's own stdin
    # to devnull, which would mask the node subprocess inheriting a live stdin.
    workflow_file = write_workflow("cat")
    caw = subprocess.Popen(
        [sys.executable, "-c", "from caw.cli import app; app()", "run", str(workflow_file)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=tmp_path,
    )

    try:
        exit_code = caw.wait(timeout=15)
    except subprocess.TimeoutExpired:
        caw.kill()
        caw.wait()
        pytest.fail("a stdin-reading node hung the run instead of completing")
    finally:
        for stream in (caw.stdin, caw.stdout, caw.stderr):
            if stream is not None:
                stream.close()

    assert exit_code == 0


@pytest.mark.asyncio
async def test_a_mid_run_crash_finalizes_state_and_appends_a_terminal_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # second needs first, so first deterministically succeeds before second's
    # spawn is attempted. (Under #4 two *independent* nodes launch concurrently,
    # so the order in which they spawn — and thus which one the forced failure
    # hits — would be a race; the edge pins it without weakening the contract:
    # the surviving node is recorded succeeded, the crashing node errored.)
    workflow = shell_workflow(("first", "echo ok", []), ("second", "echo never", ["first"]))
    real_create = asyncio.create_subprocess_shell
    spawn_count = 0

    async def failing_create(command: str, **kwargs: Any) -> asyncio.subprocess.Process:
        nonlocal spawn_count
        spawn_count += 1
        if spawn_count == 2:
            raise OSError("forced spawn failure")
        return await real_create(command, **kwargs)

    monkeypatch.setattr("caw.executor.asyncio.create_subprocess_shell", failing_create)

    with pytest.raises(OSError, match="forced spawn failure"):
        await execute_run(workflow, tmp_path / "runs")

    run_dir = single_run_dir(tmp_path / "runs")
    (run,) = state_rows(run_dir, "SELECT * FROM run")
    assert run["status"] == "errored"
    assert run["finished_at"] is not None
    assert "OSError" in run["error"] and "forced spawn failure" in run["error"]

    nodes = {row["node_id"]: row["status"] for row in state_rows(run_dir, "SELECT * FROM node")}
    assert nodes == {"first": "succeeded", "second": "errored"}

    last_event = read_events(run_dir)[-1]
    assert last_event["type"] == "run_errored"
    assert "forced spawn failure" in last_event["data"]["error"]


class _FakeProcess:
    """A subprocess stand-in that resolves communicate() to a fixed exit status.

    Used to drop a node into the same FIRST_COMPLETED batch as a peer that
    raises, without spawning a real subprocess, so the crash-drain path is
    exercised deterministically.
    """

    def __init__(self, returncode: int, on_communicate: Callable[[], None]) -> None:
        self.returncode = returncode
        self._on_communicate = on_communicate

    async def communicate(self) -> tuple[bytes, bytes]:
        self._on_communicate()
        return b"", b""

    async def wait(self) -> int:
        return self.returncode

    def kill(self) -> None:
        self.returncode = -1


@pytest.mark.asyncio
async def test_a_failed_peer_skips_its_dependents_even_when_a_sibling_crashes_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Crash-path branch-failure consistency (#54.1): `flaky` and `crash` are two
    # independent nodes launched together; `flaky` finishes with a non-zero exit
    # (an ordinary node failure) while `crash` raises (a spawn error) in the SAME
    # FIRST_COMPLETED batch. The raise crashes the run, but the invariant a failed
    # node ALWAYS skips its transitive dependents must still hold: `gated`, which
    # needs `flaky`, must be recorded `skipped` in State — never left absent.
    workflow = shell_workflow(
        ("flaky", "ignored: faked non-zero exit", []),
        ("crash", "ignored: forced spawn failure", []),
        ("gated", "echo never", ["flaky"]),
    )
    crash_may_raise = asyncio.Event()

    async def fake_create(command: str, **kwargs: Any) -> Any:
        if "non-zero exit" in command:
            # The failing peer: when its communicate() runs, release the crasher
            # so both land in the same completion batch.
            return _FakeProcess(returncode=7, on_communicate=crash_may_raise.set)
        # The crasher: wait until the failing peer has finished, then raise so the
        # raise and the non-zero exit are handled in one asyncio.wait batch.
        await crash_may_raise.wait()
        raise OSError("forced spawn failure")

    real_wait = asyncio.wait

    async def crash_first_wait(tasks: Any, **kwargs: Any) -> Any:
        # The bug surfaces only when the crashing task is processed before the
        # failed peer in the same `done` batch; `done` is a set, so its iteration
        # order is otherwise incidental (~50% repro). Return `done` as a list with
        # any raised task first to pin the worst case and make the RED determinate.
        done, pending = await real_wait(tasks, **kwargs)
        ordered = sorted(done, key=lambda task: 0 if _raised(task) else 1)
        return ordered, pending

    def _raised(task: asyncio.Future[Any]) -> bool:
        return task.done() and not task.cancelled() and task.exception() is not None

    monkeypatch.setattr("caw.executor.asyncio.create_subprocess_shell", fake_create)
    monkeypatch.setattr("caw.executor.asyncio.wait", crash_first_wait)

    with pytest.raises(OSError, match="forced spawn failure"):
        await execute_run(workflow, tmp_path / "runs")

    run_dir = single_run_dir(tmp_path / "runs")
    nodes = {row["node_id"]: row["status"] for row in state_rows(run_dir, "SELECT * FROM node")}
    assert nodes.get("flaky") == "failed", "the non-zero-exit peer is recorded failed"
    assert "gated" in nodes, "the failed peer's dependent must not be left with no State row"
    assert nodes["gated"] == "skipped", "a failed node always skips its transitive dependents"


@pytest.mark.asyncio
async def test_a_single_failing_leaf_node_with_no_dependents_fails_the_run(tmp_path: Path) -> None:
    # RunResult.succeeded correctness for a failed LEAF (#54.4): a failed Node
    # with no dependents skips nothing, so `skipped_node_ids` stays empty. The Run
    # must still be `failed` — driven by the failed Node alone, not by any skip.
    workflow = shell_workflow(("leaf", "exit 7", []))

    result = await execute_run(workflow, tmp_path / "runs")

    assert not result.succeeded, "a failed leaf fails the run with no node skipped"
    assert result.skipped_node_ids == (), "a leaf has no dependents to skip"


@pytest.mark.asyncio
async def test_a_workflow_with_concurrency_below_one_fails_loud_instead_of_vacuously_succeeding(
    tmp_path: Path,
) -> None:
    # Vacuous-success guard (#54.3): a Workflow that bypassed the `ge=1` validator
    # (model_construct / model_copy) reaches the scheduler with concurrency 0,
    # which would launch nothing and report a vacuous `succeeded` having executed
    # no Node. The scheduler must refuse it, mirroring the ordering layer's
    # bypass guard, rather than silently report success.
    valid = shell_workflow("echo hello")
    bypassed = valid.model_copy(update={"concurrency": 0})

    with pytest.raises(ValueError, match="concurrency"):
        await execute_run(bypassed, tmp_path / "runs")


@pytest.mark.asyncio
async def test_a_multi_node_concurrent_crash_records_every_in_flight_node_in_state_and_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Crash blast-radius consistency (#54.2): when several nodes are in flight as
    # the run crashes, State marks every one `errored`. The run_errored event must
    # agree — it must name every errored node, not just the first — so the Event
    # trace and State do not disagree on which nodes the crash hit.
    workflow = shell_workflow(
        ("blocker", "ignored: blocks until cancelled", []),
        ("crash", "ignored: forced spawn failure", []),
    )
    blocker_started = asyncio.Event()

    class _BlockingProcess(_FakeProcess):
        async def communicate(self) -> tuple[bytes, bytes]:
            await asyncio.Event().wait()  # blocks forever until the task is cancelled
            return b"", b""

    async def fake_create(command: str, **kwargs: Any) -> Any:
        if "blocks until cancelled" in command:
            blocker_started.set()
            return _BlockingProcess(returncode=0, on_communicate=lambda: None)
        # The crasher raises only after the blocker is in flight, so both nodes
        # are concurrently in flight when the run crashes.
        await blocker_started.wait()
        raise OSError("forced spawn failure")

    monkeypatch.setattr("caw.executor.asyncio.create_subprocess_shell", fake_create)

    with pytest.raises(OSError, match="forced spawn failure"):
        await execute_run(workflow, tmp_path / "runs")

    run_dir = single_run_dir(tmp_path / "runs")
    nodes = {row["node_id"]: row["status"] for row in state_rows(run_dir, "SELECT * FROM node")}
    assert nodes == {"blocker": "errored", "crash": "errored"}, (
        "every node in flight at the crash is marked errored in State"
    )
    last_event = read_events(run_dir)[-1]
    assert last_event["type"] == "run_errored"
    errored_in_event = set(last_event["data"]["node_ids"])
    assert errored_in_event == {"blocker", "crash"}, (
        "the run_errored event names every errored node, consistent with State"
    )


@pytest.mark.asyncio
async def test_crash_finalization_never_masks_the_original_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow = shell_workflow("echo ok")

    async def failing_create(command: str, **kwargs: Any) -> asyncio.subprocess.Process:
        raise OSError("forced spawn failure")

    def exploding_record(*args: Any, **kwargs: Any) -> None:
        # A BaseException (e.g. a second KeyboardInterrupt) raised while
        # finalizing must not replace the error that crashed the run.
        raise SystemExit(13)

    monkeypatch.setattr("caw.executor.asyncio.create_subprocess_shell", failing_create)
    monkeypatch.setattr("caw.executor.StateStore.record_run_errored", exploding_record)

    with pytest.raises(OSError, match="forced spawn failure"):
        await execute_run(workflow, tmp_path / "runs")

    # Best-effort finalization continues past the failing step: the terminal
    # event is still appended even though the State write blew up.
    run_dir = single_run_dir(tmp_path / "runs")
    assert read_events(run_dir)[-1]["type"] == "run_errored"


@pytest.mark.asyncio
async def test_cancelling_a_run_terminates_the_in_flight_node_subprocess(tmp_path: Path) -> None:
    pid_file = tmp_path / "node.pid"
    workflow = shell_workflow(f"echo $$ > {pid_file}; exec sleep 30")
    run_task = asyncio.create_task(execute_run(workflow, tmp_path / "runs"))
    for _ in range(200):
        if pid_file.exists() and pid_file.read_text().strip():
            break
        await asyncio.sleep(0.05)
    else:
        run_task.cancel()
        pytest.fail("the node subprocess never started")
    pid = int(pid_file.read_text())

    run_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_task

    for _ in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.05)
    else:
        os.kill(pid, signal.SIGKILL)
        pytest.fail(f"node subprocess {pid} was still alive after cancellation")

    # Cancellation takes the same finalization path as any other crash (issue #22).
    run_dir = single_run_dir(tmp_path / "runs")
    (run,) = state_rows(run_dir, "SELECT * FROM run")
    assert run["status"] == "errored"
    assert run["finished_at"] is not None
    assert "CancelledError" in run["error"]
    (node,) = state_rows(run_dir, "SELECT * FROM node")
    assert node["status"] == "errored"
    assert read_events(run_dir)[-1]["type"] == "run_errored"


def shell_env_workflow(node_id: str, command: str, env: list[str]) -> Workflow:
    """A single-shell-node Workflow declaring an env allow-list (#66).

    Mirrors ``policy_shell_workflow`` but carries the node-generic ``env`` field on
    a shell Node, so the shell-env parity tests can declare an allow-list without
    an inline raw dict.
    """
    raw: dict[str, Any] = {
        "name": "sample",
        "version": 1,
        "nodes": [{"id": node_id, "kind": "shell", "inputs": {"command": command, "env": env}}],
    }
    return normalize_workflow(raw, source="<test>")


@pytest.mark.asyncio
async def test_shell_node_receives_only_its_declared_env_vars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #66: a shell Node has env parity with an agent Node — only the variables it
    # declares (and that are present in the parent environment) reach the process,
    # with no parent-environment leakage. The command dumps its full environment so
    # the test can assert exactly which names crossed the seam. PATH is declared too
    # so the command's binaries resolve under the strict allow-list — exactly the
    # author-declares-what-it-needs contract the Agent-CLI seam already enforces.
    monkeypatch.setenv("DECLARED_VAR", "declared-value")
    monkeypatch.setenv("UNDECLARED_VAR", "undeclared-value")
    dump = tmp_path / "env.dump"
    workflow = shell_env_workflow("dump", f"env > {dump}", env=["DECLARED_VAR", "PATH"])

    result = await execute_run(workflow, tmp_path / "runs")

    assert result.succeeded
    lines = dump.read_text(encoding="utf-8").splitlines()
    names = {line.split("=", 1)[0] for line in lines if "=" in line}
    assert "DECLARED_VAR" in names, "the declared var reaches the shell process"
    assert "UNDECLARED_VAR" not in names, "no parent-environment leakage past the allow-list"


@pytest.mark.asyncio
async def test_shell_node_env_values_never_reach_state_events_or_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #66: the env policy keeps a declared variable's VALUE out of State, Events,
    # and the snapshot for a shell Node, exactly as for an agent Node — the policy
    # guards env injection and kernel-held values, not the command's own stdout.
    sentinel = "s3cr3t-shell-sentinel-do-not-persist"
    monkeypatch.setenv("SHELL_TOKEN", sentinel)
    # The command consumes the var without echoing it, so the value is exercised
    # but never surfaced by the node's own output.
    workflow = shell_env_workflow("consume", 'test -n "$SHELL_TOKEN"', env=["SHELL_TOKEN"])

    result = await execute_run(workflow, tmp_path / "runs")

    assert result.succeeded, "the declared var was present in the shell process"
    run_dir = single_run_dir(tmp_path / "runs")
    state_bytes = (run_dir / "state.sqlite").read_bytes()
    events_text = (run_dir / "events.jsonl").read_text(encoding="utf-8")
    snapshot_text = (run_dir / "workflow.normalized.json").read_text(encoding="utf-8")
    assert sentinel.encode() not in state_bytes, "the secret value must not reach State"
    assert sentinel not in events_text, "the secret value must not reach Events"
    assert sentinel not in snapshot_text, "the secret value must not reach the snapshot"


@pytest.mark.asyncio
async def test_shell_node_with_no_declared_env_inherits_the_parent_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #66: the allow-list ENGAGES only when a shell Node declares `env`. With no
    # `env` declared (the default), the shell inherits the parent environment
    # unchanged — preserving the pre-#66 behavior so existing shell workflows that
    # rely on inherited PATH and ambient vars keep working.
    monkeypatch.setenv("AMBIENT_VAR", "ambient-value")
    dump = tmp_path / "env.dump"
    workflow = shell_workflow(("dump", f"env > {dump}", []))

    result = await execute_run(workflow, tmp_path / "runs")

    assert result.succeeded
    names = {
        line.split("=", 1)[0]
        for line in dump.read_text(encoding="utf-8").splitlines()
        if "=" in line
    }
    assert "AMBIENT_VAR" in names, "an undeclared shell Node inherits the parent environment"
