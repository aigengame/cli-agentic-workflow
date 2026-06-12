"""Executor-seam tests: drive execute_run directly on the asyncio Engine Backend."""

import asyncio
import os
import signal
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from caw.executor import execute_run
from caw.model import Workflow, normalize_workflow


def shell_workflow(*commands: str) -> Workflow:
    raw: dict[str, Any] = {
        "name": "sample",
        "version": 1,
        "nodes": [
            {"id": f"node{index}", "kind": "shell", "inputs": {"command": command}}
            for index, command in enumerate(commands, start=1)
        ],
    }
    return normalize_workflow(raw, source="<test>")


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
