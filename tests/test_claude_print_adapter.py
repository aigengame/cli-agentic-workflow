"""Offline tests for the real ``claude.print`` Adapter (#9).

These prove the Adapter normalizes ``claude -p`` invocations into vendor-neutral
:class:`AgentResult`s and reports an actionable setup error for a missing CLI,
WITHOUT a real ``claude`` on PATH: the subprocess spawn and the version probe are
the only seams, and they are monkeypatched here. A separate online test
(``test_claude_print_real_cli``) exercises a real ``claude`` and auto-skips when
the CLI is absent.
"""

import pytest

from caw.adapter import AdapterError, AgentInvocation
from caw.claude_print import ClaudePrintAdapter


class FakeProcess:
    """A stand-in for ``asyncio.subprocess.Process`` recording its spawn call."""

    def __init__(self, returncode: int, stdout: bytes = b"", stderr: bytes = b"") -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr


def patch_spawn(monkeypatch: pytest.MonkeyPatch, process: FakeProcess) -> dict[str, object]:
    """Patch ``asyncio.create_subprocess_exec`` to return ``process``; record args/env."""
    captured: dict[str, object] = {}

    async def fake_exec(*args: object, **kwargs: object) -> FakeProcess:
        captured["args"] = args
        captured["env"] = kwargs.get("env")
        return process

    monkeypatch.setattr("caw.claude_print.asyncio.create_subprocess_exec", fake_exec)
    return captured


@pytest.mark.asyncio
async def test_zero_exit_normalizes_stdout_and_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_spawn(monkeypatch, FakeProcess(0, stdout=b"a one-line summary", stderr=b""))
    adapter = ClaudePrintAdapter()

    result = await adapter.invoke(
        AgentInvocation(node_id="n", adapter="claude.print", prompt="summarize")
    )

    assert result.exit_status == 0
    assert result.stdout == "a one-line summary"
    assert result.stderr == ""
    assert result.structured_output is None
    assert result.artifacts == ()


@pytest.mark.asyncio
async def test_non_zero_exit_is_an_ordinary_result_not_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ADR 0006: a `claude` process that RAN and exited non-zero is a normal
    # AgentResult(exit_status=N), never an AdapterError. AdapterError is reserved
    # for the Adapter being unable to produce a result at all.
    patch_spawn(monkeypatch, FakeProcess(7, stdout=b"partial", stderr=b"the model refused"))
    adapter = ClaudePrintAdapter()

    result = await adapter.invoke(
        AgentInvocation(node_id="n", adapter="claude.print", prompt="do it")
    )

    assert result.exit_status == 7
    assert result.stdout == "partial"
    assert result.stderr == "the model refused"
    assert result.structured_output is None


@pytest.mark.asyncio
async def test_missing_cli_raises_an_actionable_setup_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A missing `claude` on PATH must be an ACTIONABLE AdapterError (a setup
    # message telling the user how to install/enable it), NOT a raw
    # FileNotFoundError that escapes the Adapter.
    async def raise_not_found(*args: object, **kwargs: object) -> object:
        raise FileNotFoundError(2, "No such file or directory", "claude")

    monkeypatch.setattr("caw.claude_print.asyncio.create_subprocess_exec", raise_not_found)
    adapter = ClaudePrintAdapter()

    with pytest.raises(AdapterError) as excinfo:
        await adapter.invoke(AgentInvocation(node_id="n", adapter="claude.print", prompt="do it"))

    message = str(excinfo.value)
    assert "claude" in message
    assert "install" in message.lower(), "the error tells the user how to install/enable the CLI"
