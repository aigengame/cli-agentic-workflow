"""Offline tests for the shared SubprocessAdapter base and its JSON ladder (#83).

These pin OUR-logic of the shared subprocess infrastructure directly — the CLI is
located ONCE and cached, the spawn passes the strict env / stdin isolation / process
group, a missing CLI is an actionable error, a signal-kill returncode is passed
through unchanged, and the shared read -> json.loads -> dict AdapterError ladder
raises the same shape for a file (mock fixture) and a string (CLI wrapper). The spawn
and `which` are the only seams, monkeypatched here; real-CLI behavior lives in the
e2e tier (#86).
"""

import asyncio
from pathlib import Path

import pytest

from caw.adapter import AdapterError, AgentInvocation
from caw.subprocess_adapter import (
    CompletedSubprocess,
    SubprocessAdapter,
    node_context,
    parse_json_object,
    read_json_object,
)

FAKE_CLI_PATH = "/fake/abs/bin/probe"


class FakeProcess:
    """A stand-in for ``asyncio.subprocess.Process`` for the base's spawn seam."""

    def __init__(self, returncode: int, stdout: bytes = b"", stderr: bytes = b"") -> None:
        self.returncode = returncode
        self.pid = 4242
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr

    async def wait(self) -> int:
        return self.returncode


class _ProbeAdapter(SubprocessAdapter):
    """A minimal concrete SubprocessAdapter for exercising the base in isolation."""

    cli_name = "probe"
    missing_cli_hint = "the 'probe' CLI was not found on PATH. install it."


def patch_which(
    monkeypatch: pytest.MonkeyPatch, resolved: str | None = FAKE_CLI_PATH
) -> list[str]:
    """Patch ``shutil.which`` in the base namespace; record each lookup's name."""
    calls: list[str] = []

    def fake_which(name: str) -> str | None:
        calls.append(name)
        return resolved

    monkeypatch.setattr("caw.subprocess_adapter.shutil.which", fake_which)
    return calls


def patch_spawn(monkeypatch: pytest.MonkeyPatch, process: FakeProcess) -> dict[str, object]:
    """Patch ``asyncio.create_subprocess_exec`` in the base namespace; record kwargs."""
    captured: dict[str, object] = {}

    async def fake_exec(*args: object, **kwargs: object) -> FakeProcess:
        captured["args"] = args
        captured["env"] = kwargs.get("env")
        captured["kwargs"] = kwargs
        return process

    monkeypatch.setattr("caw.subprocess_adapter.asyncio.create_subprocess_exec", fake_exec)
    return captured


@pytest.mark.asyncio
async def test_cli_path_is_resolved_once_and_cached_across_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # AC (#83): the CLI is located via shutil.which ONCE per instance and cached, so
    # repeated invocations do not re-probe PATH. Two run_cli calls on one adapter
    # resolve `which` exactly once.
    which_calls = patch_which(monkeypatch)
    patch_spawn(monkeypatch, FakeProcess(0, stdout=b"ok"))
    adapter = _ProbeAdapter()

    path1 = adapter.resolve_cli_path("ctx")
    path2 = adapter.resolve_cli_path("ctx")

    assert path1 == path2 == FAKE_CLI_PATH
    assert which_calls == ["probe"], "the CLI is located via shutil.which exactly once"


@pytest.mark.asyncio
async def test_run_cli_passes_strict_env_isolated_stdin_and_a_private_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The base spawns with EXACTLY the supplied env (the node's allow-list), DEVNULL
    # stdin, and start_new_session so the whole tree is killable by group.
    patch_which(monkeypatch)
    captured = patch_spawn(monkeypatch, FakeProcess(0, stdout=b"ok"))
    adapter = _ProbeAdapter()

    await adapter.run_cli([FAKE_CLI_PATH, "--flag"], context_label="ctx", env={"DECLARED": "v"})

    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert captured["env"] == {"DECLARED": "v"}, "exactly the supplied allow-list reaches it"
    assert kwargs.get("stdin") == asyncio.subprocess.DEVNULL
    assert kwargs.get("start_new_session") is True


@pytest.mark.asyncio
async def test_run_cli_with_no_env_passes_the_ambient_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An infrastructure probe (env=None, e.g. capability_check) passes NO env= so it
    # may use the ambient environment to locate/run the CLI — distinct from an invoke,
    # which passes the node's filtered allow-list.
    patch_which(monkeypatch)
    captured = patch_spawn(monkeypatch, FakeProcess(0, stdout=b"v1.0"))
    adapter = _ProbeAdapter()

    await adapter.run_cli([FAKE_CLI_PATH, "--version"], context_label="capability check")

    assert captured["env"] is None, "an infrastructure probe passes no filtered env"


@pytest.mark.asyncio
async def test_run_cli_passes_a_signal_kill_returncode_through_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #84: a negative signal-kill returncode (e.g. -9) is reported as-is, never coerced
    # to the executor's -1 TIMED_OUT sentinel.
    patch_which(monkeypatch)
    patch_spawn(monkeypatch, FakeProcess(-9, stdout=b"", stderr=b"Killed"))
    adapter = _ProbeAdapter()

    completed = await adapter.run_cli([FAKE_CLI_PATH], context_label="ctx")

    assert completed == CompletedSubprocess(returncode=-9, stdout="", stderr="Killed")


@pytest.mark.asyncio
async def test_missing_cli_is_an_actionable_setup_error_before_spawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A missing CLI (shutil.which -> None) raises the subclass's actionable hint
    # BEFORE any spawn — locating the tool is separate from running it.
    patch_which(monkeypatch, resolved=None)

    async def fail_if_spawned(*args: object, **kwargs: object) -> object:
        raise AssertionError("a missing CLI must error before create_subprocess_exec")

    monkeypatch.setattr("caw.subprocess_adapter.asyncio.create_subprocess_exec", fail_if_spawned)
    adapter = _ProbeAdapter()

    with pytest.raises(AdapterError) as excinfo:
        adapter.resolve_cli_path("ctx")

    assert "probe" in str(excinfo.value)


@pytest.mark.asyncio
async def test_capability_check_returns_the_version_and_uses_the_ambient_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The base's version probe spawns `<cli> --version` with no filtered env= and
    # returns the stripped stdout — shared by claude (#9) and codex (#11).
    patch_which(monkeypatch)
    captured = patch_spawn(monkeypatch, FakeProcess(0, stdout=b"1.2.3\n"))
    adapter = _ProbeAdapter()

    version = await adapter.capability_check()

    assert version == "1.2.3"
    assert captured["args"] == (FAKE_CLI_PATH, "--version")
    assert captured["env"] is None


@pytest.mark.asyncio
async def test_capability_check_nonzero_exit_is_an_adapter_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_which(monkeypatch)
    patch_spawn(monkeypatch, FakeProcess(2, stdout=b"", stderr=b"broken install"))
    adapter = _ProbeAdapter()

    with pytest.raises(AdapterError) as excinfo:
        await adapter.capability_check()

    assert "broken install" in str(excinfo.value)


def test_parse_json_object_and_read_json_object_share_the_ladder(tmp_path: Path) -> None:
    # The shared JSON ladder raises a node-context AdapterError for unparseable JSON
    # and a non-object payload, for BOTH a string (CLI wrapper) and a file (fixture).
    invocation = AgentInvocation(node_id="n", adapter="mock", prompt="p")
    ctx = node_context(invocation)

    assert parse_json_object('{"a": 1}', context=ctx, source_label="wrapper") == {"a": 1}
    with pytest.raises(AdapterError):
        parse_json_object("not json", context=ctx, source_label="wrapper")
    with pytest.raises(AdapterError):
        parse_json_object("[1, 2]", context=ctx, source_label="wrapper")  # not an object

    good = tmp_path / "good.json"
    good.write_text('{"b": 2}', encoding="utf-8")
    assert read_json_object(good, context=ctx, source_label="fixture") == {"b": 2}
    bad = tmp_path / "bad.json"
    bad.write_text("nope", encoding="utf-8")
    with pytest.raises(AdapterError):
        read_json_object(bad, context=ctx, source_label="fixture")
    with pytest.raises(AdapterError):
        read_json_object(tmp_path / "missing.json", context=ctx, source_label="fixture")
