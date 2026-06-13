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
