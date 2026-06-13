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

from caw.executor import execute_run
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
