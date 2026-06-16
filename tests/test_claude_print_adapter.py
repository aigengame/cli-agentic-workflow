"""Offline tests for the real ``claude.print`` Adapter (#9).

These prove the Adapter normalizes ``claude -p`` invocations into vendor-neutral
:class:`AgentResult`s and reports an actionable setup error for a missing CLI,
WITHOUT a real ``claude`` on PATH: the subprocess spawn and the version probe are
the only seams, and they are monkeypatched here. Real-CLI coverage lives in the
``e2e`` tier (``tests/e2e/``), which runs a real ``claude`` locally and FAILS — never
skips — when the CLI is unavailable (#86).
"""

import asyncio
import json
import signal
import sqlite3
from pathlib import Path

import pytest
from conftest import write_schema

from caw.adapter import AdapterError, AdapterRegistry, AgentInvocation
from caw.claude_print import ClaudePrintAdapter
from caw.executor import execute_run
from caw.model import normalize_workflow


def claude_json_result(result: str = "ok", **fields: object) -> bytes:
    """Build the wrapper JSON object `claude -p --output-format json` prints.

    Mirrors the real CLI shape: a single JSON object with `type: "result"`, the
    freeform `result` text, and (when a `--json-schema` was supplied) a top-level
    `structured_output` field. Encoded to bytes as the subprocess would emit it.
    """
    payload: dict[str, object] = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": result,
    }
    payload.update(fields)
    return json.dumps(payload).encode("utf-8")


class FakeProcess:
    """A stand-in for ``asyncio.subprocess.Process`` recording its spawn call.

    ``communicate_raises`` lets a test model the kernel's ``asyncio.timeout``
    cancelling the awaited ``invoke`` (``communicate`` raises ``CancelledError`` /
    ``TimeoutError``). A still-running process (``returncode=None``) records whether
    its tree was killed and reaped via ``pid``/``wait``, so a test can assert the
    adapter cleans up instead of orphaning the subprocess.
    """

    def __init__(
        self,
        returncode: int | None,
        stdout: bytes = b"",
        stderr: bytes = b"",
        communicate_raises: BaseException | None = None,
    ) -> None:
        self.returncode = returncode
        self.pid = 4242
        self._stdout = stdout
        self._stderr = stderr
        self._communicate_raises = communicate_raises
        self.waited = False

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        if self._communicate_raises is not None:
            raise self._communicate_raises
        return self._stdout, self._stderr

    async def wait(self) -> int:
        # Reaping a killed process: record the reap and settle the returncode so a
        # second kill is a no-op, mirroring asyncio.subprocess.Process.wait().
        self.waited = True
        if self.returncode is None:
            self.returncode = -9
        return self.returncode


FAKE_CLAUDE_PATH = "/fake/abs/bin/claude"


def patch_killpg(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int]]:
    """Patch ``os.killpg`` in the adapter namespace; record (pid, signal) calls."""
    calls: list[tuple[int, int]] = []

    def fake_killpg(pid: int, sig: int) -> None:
        calls.append((pid, sig))

    monkeypatch.setattr("caw.claude_print.os.killpg", fake_killpg)
    return calls


def patch_spawn(monkeypatch: pytest.MonkeyPatch, process: FakeProcess) -> dict[str, object]:
    """Patch ``asyncio.create_subprocess_exec`` to return ``process``; record args/env."""
    captured: dict[str, object] = {}

    async def fake_exec(*args: object, **kwargs: object) -> FakeProcess:
        captured["args"] = args
        captured["env"] = kwargs.get("env")
        captured["kwargs"] = kwargs
        return process

    monkeypatch.setattr("caw.claude_print.asyncio.create_subprocess_exec", fake_exec)
    return captured


def patch_which(monkeypatch: pytest.MonkeyPatch, resolved: str | None = FAKE_CLAUDE_PATH) -> None:
    """Patch ``shutil.which`` (in the adapter's namespace) to return ``resolved``.

    Locating the CLI is infrastructure that uses the ambient environment, so the
    offline suite stubs the lookup rather than depending on a real ``claude`` on
    PATH. ``resolved=None`` models a missing CLI.
    """
    monkeypatch.setattr("caw.claude_print.shutil.which", lambda _name: resolved)


@pytest.mark.asyncio
async def test_zero_exit_normalizes_stdout_and_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_which(monkeypatch)
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
    patch_which(monkeypatch)
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
async def test_invalid_utf8_bytes_decode_recoverably_not_with_replacement_chars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # stdout/stderr feed State and downstream `when` predicates, so an undecodable
    # byte must decode RECOVERABLY (backslashreplace -> `\xff`), like the executor's
    # shell node, not irreversibly (replace -> U+FFFD which loses the original byte).
    patch_which(monkeypatch)
    patch_spawn(monkeypatch, FakeProcess(0, stdout=b"out\xff", stderr=b"err\xfe"))
    adapter = ClaudePrintAdapter()

    result = await adapter.invoke(
        AgentInvocation(node_id="n", adapter="claude.print", prompt="do it")
    )

    assert "�" not in result.stdout and "�" not in result.stderr, "no lossy U+FFFD"
    assert result.stdout == "out\\xff"
    assert result.stderr == "err\\xfe"


@pytest.mark.asyncio
async def test_missing_cli_raises_an_actionable_setup_error_before_spawning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # When `claude` is not on PATH (shutil.which returns None), invoke raises an
    # ACTIONABLE AdapterError (a setup message telling the user how to install/enable
    # it) BEFORE attempting to spawn — separating locate-the-tool from run-the-tool.
    patch_which(monkeypatch, resolved=None)

    async def fail_if_spawned(*args: object, **kwargs: object) -> object:
        raise AssertionError("a missing CLI must error before create_subprocess_exec")

    monkeypatch.setattr("caw.claude_print.asyncio.create_subprocess_exec", fail_if_spawned)
    adapter = ClaudePrintAdapter()

    with pytest.raises(AdapterError) as excinfo:
        await adapter.invoke(AgentInvocation(node_id="n", adapter="claude.print", prompt="do it"))

    message = str(excinfo.value)
    assert "claude" in message
    assert "install" in message.lower(), "the error tells the user how to install/enable the CLI"


@pytest.mark.asyncio
async def test_invoke_spawns_in_its_own_session_with_isolated_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The spawn must start a new session (so the whole process tree can be signalled
    # by process group on cancellation/timeout) and isolate stdin (DEVNULL) so the
    # child cannot block reading the parent's stdin — mirroring the executor's shell
    # node.
    patch_which(monkeypatch)
    captured = patch_spawn(monkeypatch, FakeProcess(0, stdout=b"ok"))
    adapter = ClaudePrintAdapter()

    await adapter.invoke(AgentInvocation(node_id="n", adapter="claude.print", prompt="p"))

    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs.get("start_new_session") is True
    assert kwargs.get("stdin") == asyncio.subprocess.DEVNULL


@pytest.mark.asyncio
async def test_capability_check_spawns_in_its_own_session_with_isolated_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The version probe spawns the same way: a new session for process-group teardown
    # on cancellation, and isolated stdin so the probe never blocks on parent stdin.
    patch_which(monkeypatch)
    captured = patch_spawn(monkeypatch, FakeProcess(0, stdout=b"2.1.0\n"))
    adapter = ClaudePrintAdapter()

    await adapter.capability_check()

    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs.get("start_new_session") is True
    assert kwargs.get("stdin") == asyncio.subprocess.DEVNULL


@pytest.mark.asyncio
async def test_invoke_kills_and_reaps_the_subprocess_tree_on_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # When the kernel's asyncio.timeout cancels the awaited invoke, CancelledError is
    # raised at communicate(). The adapter must KILL the whole process tree by group
    # (os.killpg ... SIGKILL) and REAP it (wait), leaving no orphan, then re-raise —
    # mirroring the executor's _kill_and_reap. A still-running process has
    # returncode=None.
    patch_which(monkeypatch)
    killed = patch_killpg(monkeypatch)
    process = FakeProcess(None, communicate_raises=asyncio.CancelledError())
    patch_spawn(monkeypatch, process)
    adapter = ClaudePrintAdapter()

    with pytest.raises(asyncio.CancelledError):
        await adapter.invoke(AgentInvocation(node_id="n", adapter="claude.print", prompt="p"))

    assert killed == [(process.pid, signal.SIGKILL)], "the process tree is killed by group"
    assert process.waited is True, "the killed process is reaped (no orphan)"


@pytest.mark.asyncio
async def test_invoke_kills_and_reaps_on_timeout_error_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A TimeoutError surfacing at communicate() (BaseException path) is cleaned up the
    # same way and re-raised, so the executor's TIMED_OUT classification still sees it.
    patch_which(monkeypatch)
    killed = patch_killpg(monkeypatch)
    process = FakeProcess(None, communicate_raises=TimeoutError())
    patch_spawn(monkeypatch, process)
    adapter = ClaudePrintAdapter()

    with pytest.raises(TimeoutError):
        await adapter.invoke(AgentInvocation(node_id="n", adapter="claude.print", prompt="p"))

    assert killed and process.waited is True


@pytest.mark.asyncio
async def test_invoke_cancellation_kills_group_even_if_leader_already_exited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The leader's `returncode` being set does NOT mean the process GROUP is dead: a
    # grandchild can have inherited the stdout/stderr pipe and still be alive, keeping
    # `communicate()` blocked until cancellation surfaces. So on cancellation the
    # group is ALWAYS signalled (os.killpg ... SIGKILL) — regardless of the leader's
    # returncode — and then reaped, before re-raising. Killing only when returncode
    # is None would orphan that surviving grandchild.
    patch_which(monkeypatch)
    killed = patch_killpg(monkeypatch)
    process = FakeProcess(0, communicate_raises=asyncio.CancelledError())
    patch_spawn(monkeypatch, process)
    adapter = ClaudePrintAdapter()

    with pytest.raises(asyncio.CancelledError):
        await adapter.invoke(AgentInvocation(node_id="n", adapter="claude.print", prompt="p"))

    assert killed == [(process.pid, signal.SIGKILL)], (
        "the group is killed even though the leader already exited (a grandchild may survive)"
    )
    assert process.waited is True, "it is still reaped"


@pytest.mark.asyncio
async def test_invoke_cancellation_suppresses_process_lookup_when_group_is_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # When the WHOLE process group is already gone, os.killpg raises
    # ProcessLookupError. That race must be suppressed (there is nothing left to
    # kill) and the original exception must still propagate after reaping.
    patch_which(monkeypatch)

    def killpg_no_such_group(pid: int, sig: int) -> None:
        raise ProcessLookupError(3, "No such process")

    monkeypatch.setattr("caw.claude_print.os.killpg", killpg_no_such_group)
    process = FakeProcess(None, communicate_raises=asyncio.CancelledError())
    patch_spawn(monkeypatch, process)
    adapter = ClaudePrintAdapter()

    with pytest.raises(asyncio.CancelledError):
        await adapter.invoke(AgentInvocation(node_id="n", adapter="claude.print", prompt="p"))

    assert process.waited is True, "the process is still reaped after the suppressed lookup error"


@pytest.mark.asyncio
async def test_capability_check_kills_and_reaps_the_subprocess_on_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The version probe gets the same cleanup: cancellation at communicate() kills the
    # tree by group and reaps it before re-raising, so a cancelled capability check
    # leaves no orphan probe process.
    patch_which(monkeypatch)
    killed = patch_killpg(monkeypatch)
    process = FakeProcess(None, communicate_raises=asyncio.CancelledError())
    patch_spawn(monkeypatch, process)
    adapter = ClaudePrintAdapter()

    with pytest.raises(asyncio.CancelledError):
        await adapter.capability_check()

    assert killed == [(process.pid, signal.SIGKILL)]
    assert process.waited is True


@pytest.mark.asyncio
async def test_filenotfound_at_spawn_is_translated_as_a_toctou_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Defense-in-depth: shutil.which resolved a path, but the binary vanished before
    # the spawn (a TOCTOU race). A raw FileNotFoundError must NOT escape the Adapter;
    # it is translated into the same actionable setup AdapterError.
    patch_which(monkeypatch)

    async def raise_not_found(*args: object, **kwargs: object) -> object:
        raise FileNotFoundError(2, "No such file or directory", FAKE_CLAUDE_PATH)

    monkeypatch.setattr("caw.claude_print.asyncio.create_subprocess_exec", raise_not_found)
    adapter = ClaudePrintAdapter()

    with pytest.raises(AdapterError) as excinfo:
        await adapter.invoke(AgentInvocation(node_id="n", adapter="claude.print", prompt="do it"))

    message = str(excinfo.value)
    assert "claude" in message
    assert "install" in message.lower(), "the error tells the user how to install/enable the CLI"


@pytest.mark.asyncio
async def test_invoke_spawns_the_resolved_absolute_cli_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Locating the tool is infrastructure (use the ambient PATH), separate from the
    # child's env policy. invoke resolves `claude` to an ABSOLUTE path via
    # shutil.which and spawns THAT path, so OS executable lookup never depends on a
    # PATH in the node's env allow-list. argv[1:] is unchanged.
    patch_which(monkeypatch)
    captured = patch_spawn(monkeypatch, FakeProcess(0, stdout=b"ok"))
    adapter = ClaudePrintAdapter()

    await adapter.invoke(AgentInvocation(node_id="n", adapter="claude.print", prompt="summarize"))

    argv = captured["args"]
    assert isinstance(argv, tuple)
    assert argv[0] == FAKE_CLAUDE_PATH, "argv[0] is the which-resolved absolute path"
    assert argv[1:] == ("-p", "--", "summarize"), "the prompt is last, after the `--` separator"


@pytest.mark.asyncio
async def test_prompt_is_positional_and_node_args_pass_through_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The prompt is the trailing positional argument to `claude -p`, placed AFTER a
    # `--` end-of-options separator; the node's `args` (sandbox/approval/any other CLI
    # flags) pass through to the subprocess UNCHANGED, BEFORE the `--`. caw adds no
    # policy engine: it neither interprets nor injects flags. argv[0] is the
    # which-resolved absolute path; the passthrough flags precede `--`, the prompt
    # follows it.
    patch_which(monkeypatch)
    captured = patch_spawn(monkeypatch, FakeProcess(0, stdout=b"ok"))
    adapter = ClaudePrintAdapter()

    await adapter.invoke(
        AgentInvocation(
            node_id="n",
            adapter="claude.print",
            prompt="summarize the repo",
            args=("--permission-mode", "acceptEdits", "--add-dir", "/tmp/x"),
        )
    )

    assert captured["args"] == (
        FAKE_CLAUDE_PATH,
        "-p",
        "--permission-mode",
        "acceptEdits",
        "--add-dir",
        "/tmp/x",
        "--",
        "summarize the repo",
    )


@pytest.mark.asyncio
async def test_leading_dash_prompt_is_protected_by_the_end_of_options_separator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A prompt that begins with `-` (e.g. "--help") must NEVER be parsed by `claude`
    # as a flag (`claude -p --help` would print help). The `--` end-of-options
    # separator guarantees the prompt is treated as a positional: `--` is present in
    # argv and the prompt is the LAST argv element, so nothing after it can be read
    # as an option.
    patch_which(monkeypatch)
    captured = patch_spawn(monkeypatch, FakeProcess(0, stdout=b"ok"))
    adapter = ClaudePrintAdapter()

    await adapter.invoke(AgentInvocation(node_id="n", adapter="claude.print", prompt="--help"))

    argv = captured["args"]
    assert isinstance(argv, tuple)
    assert "--" in argv, "the end-of-options separator protects a leading-dash prompt"
    assert argv[-1] == "--help", "the prompt is the last argv element, never parsed as a flag"
    assert argv[argv.index("--") + 1] == "--help", "the prompt immediately follows the separator"


@pytest.mark.asyncio
async def test_only_the_invocation_env_reaches_the_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Env policy (ADR 0006, #5): the executor hands the adapter an already-filtered
    # allow-list. The adapter passes EXACTLY that to the subprocess — never merging
    # the parent os.environ, which would leak the whole environment.
    #
    # Crucially the node env LACKS PATH (the normal case), yet invoke still LOCATES
    # the CLI because shutil.which uses the ambient env (infrastructure) — separate
    # from the child's env policy. The fix must NOT reintroduce parent-env leakage:
    # the child still receives EXACTLY {"DECLARED_VAR": "..."} and no PATH.
    monkeypatch.setenv("PARENT_ONLY_VAR", "leaked-if-present")
    patch_which(monkeypatch)
    captured = patch_spawn(monkeypatch, FakeProcess(0, stdout=b"ok"))
    adapter = ClaudePrintAdapter()

    await adapter.invoke(
        AgentInvocation(
            node_id="n",
            adapter="claude.print",
            prompt="do it",
            env={"DECLARED_VAR": "declared-value"},
        )
    )

    assert captured["env"] == {"DECLARED_VAR": "declared-value"}, (
        "exactly the invocation env reaches the subprocess, with no parent-env leakage"
    )
    assert "PATH" not in captured["env"], "locating the CLI does not inject PATH into the child"


@pytest.mark.asyncio
async def test_output_schema_requests_json_and_parses_structured_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # When the node declares an output_schema, the adapter REQUESTS JSON output
    # from the CLI (`--output-format json --json-schema <schema>`) and parses the
    # CLI's top-level `structured_output` field into AgentResult.structured_output.
    # The KERNEL validates the schema afterward; the adapter does NOT validate it.
    schema = write_schema(
        tmp_path / "person.schema.json",
        {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
    )
    stdout = claude_json_result(
        result="Returned a person.", structured_output={"name": "Alice", "age": 30}
    )
    patch_which(monkeypatch)
    captured = patch_spawn(monkeypatch, FakeProcess(0, stdout=stdout))
    adapter = ClaudePrintAdapter()

    result = await adapter.invoke(
        AgentInvocation(
            node_id="n", adapter="claude.print", prompt="make a person", output_schema=schema
        )
    )

    argv = captured["args"]
    assert isinstance(argv, tuple)
    assert "--output-format" in argv and argv[argv.index("--output-format") + 1] == "json"
    schema_text = schema.read_text(encoding="utf-8")
    assert "--json-schema" in argv and argv[argv.index("--json-schema") + 1] == schema_text
    # The structured flags precede the `--` separator; the prompt is still last.
    assert argv == (
        FAKE_CLAUDE_PATH,
        "-p",
        "--output-format",
        "json",
        "--json-schema",
        schema_text,
        "--",
        "make a person",
    )
    assert result.exit_status == 0
    assert result.structured_output == {"name": "Alice", "age": 30}


@pytest.mark.asyncio
async def test_explicit_json_null_structured_output_passes_through_as_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # When the wrapper carries an EXPLICIT `structured_output: null`, the adapter
    # passes it through as Python None — it does NOT raise. The kernel's schema is
    # the sole arbiter of whether null satisfies the Output Contract (ADR 0006); a
    # schema permitting null passes, one requiring content fails downstream.
    schema = write_schema(tmp_path / "s.schema.json", {"type": ["object", "null"]})
    stdout = claude_json_result(result="produced null", structured_output=None)
    patch_which(monkeypatch)
    patch_spawn(monkeypatch, FakeProcess(0, stdout=stdout))
    adapter = ClaudePrintAdapter()

    result = await adapter.invoke(
        AgentInvocation(node_id="n", adapter="claude.print", prompt="p", output_schema=schema)
    )

    assert result.exit_status == 0
    assert result.structured_output is None


@pytest.mark.asyncio
async def test_absent_structured_output_key_when_required_is_an_adapter_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A zero-exit, non-is_error structured run whose wrapper has NO
    # `structured_output` key means the CLI was asked for structured output (via
    # --json-schema) but produced none. The adapter cannot produce a result — an
    # AdapterError, consistent with the unparseable-when-required case. This must NOT
    # collapse an ABSENT key to None (which would defeat the kernel's null-vs-absent
    # distinction); the explicit-null case is handled separately.
    schema = write_schema(tmp_path / "s.schema.json", {"type": "object"})
    stdout = claude_json_result(result="forgot the structured output")  # no structured_output key
    patch_which(monkeypatch)
    patch_spawn(monkeypatch, FakeProcess(0, stdout=stdout))
    adapter = ClaudePrintAdapter()

    with pytest.raises(AdapterError) as excinfo:
        await adapter.invoke(
            AgentInvocation(node_id="n", adapter="claude.print", prompt="p", output_schema=schema)
        )

    message = str(excinfo.value)
    assert "n" in message
    assert "structured_output" in message


@pytest.mark.asyncio
async def test_no_output_schema_does_not_request_json_and_leaves_structured_output_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Without an output_schema the adapter does NOT request JSON output: stdout is
    # the CLI's raw text and structured_output stays None.
    patch_which(monkeypatch)
    captured = patch_spawn(monkeypatch, FakeProcess(0, stdout=b"plain text answer"))
    adapter = ClaudePrintAdapter()

    result = await adapter.invoke(
        AgentInvocation(node_id="n", adapter="claude.print", prompt="answer")
    )

    argv = captured["args"]
    assert isinstance(argv, tuple)
    assert "--output-format" not in argv and "--json-schema" not in argv
    assert result.stdout == "plain text answer"
    assert result.structured_output is None


@pytest.mark.asyncio
async def test_unparseable_json_when_a_schema_was_required_is_an_adapter_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # When the node required structured output but the CLI exited zero with stdout
    # that is not the expected JSON wrapper, the adapter cannot produce a result:
    # ADR 0006 reserves AdapterError for exactly this (output required, unparseable).
    schema = write_schema(tmp_path / "s.schema.json", {"type": "object"})
    patch_which(monkeypatch)
    patch_spawn(monkeypatch, FakeProcess(0, stdout=b"not json at all"))
    adapter = ClaudePrintAdapter()

    with pytest.raises(AdapterError) as excinfo:
        await adapter.invoke(
            AgentInvocation(node_id="n", adapter="claude.print", prompt="p", output_schema=schema)
        )

    assert "n" in str(excinfo.value)


@pytest.mark.asyncio
async def test_non_zero_exit_with_schema_does_not_attempt_to_parse_structured_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A non-zero exit is an ordinary AgentResult even when a schema was declared:
    # the adapter must NOT raise an AdapterError for unparseable output here — the
    # process failed and its exit status is the node's failure, parseability moot.
    schema = write_schema(tmp_path / "s.schema.json", {"type": "object"})
    patch_which(monkeypatch)
    patch_spawn(monkeypatch, FakeProcess(2, stdout=b"error text, not json", stderr=b"boom"))
    adapter = ClaudePrintAdapter()

    result = await adapter.invoke(
        AgentInvocation(node_id="n", adapter="claude.print", prompt="p", output_schema=schema)
    )

    assert result.exit_status == 2
    assert result.structured_output is None
    assert result.stderr == "boom"


@pytest.mark.asyncio
async def test_zero_exit_but_wrapper_reports_error_normalizes_to_a_failed_node(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Defense-in-depth (#9 review follow-up): `claude` is EXPECTED to exit non-zero
    # when the wrapper says `is_error: true`, so the exit code already catches the
    # common case. But some error subtypes can accompany a ZERO exit, so on the
    # structured path the adapter also inspects the wrapper: exit 0 + is_error true
    # is normalized to a FAILED node via the FIRST-CLASS `adapter_failure` signal
    # (#83) — NOT by manufacturing a non-zero exit_status. The adapter keeps the
    # process's REAL exit_status (here 0), raises `adapter_failure`, drops the
    # structured_output (a failed node carries no trustworthy output), and appends
    # an actionable annotation to stderr. The kernel honors the flag once.
    schema = write_schema(tmp_path / "s.schema.json", {"type": "object"})
    stdout = claude_json_result(
        result="partial work",
        is_error=True,
        structured_output={"name": "Alice"},
    )
    patch_which(monkeypatch)
    patch_spawn(monkeypatch, FakeProcess(0, stdout=stdout))
    adapter = ClaudePrintAdapter()

    result = await adapter.invoke(
        AgentInvocation(node_id="n", adapter="claude.print", prompt="p", output_schema=schema)
    )

    assert result.adapter_failure is True, "the failure rides the first-class signal"
    assert result.exit_status == 0, "the real process exit_status is preserved, not fabricated"
    assert result.structured_output is None
    assert "error" in result.stderr.lower(), "stderr carries an actionable CLI-error annotation"
    # The raw JSON wrapper is preserved in stdout so the trace retains full CLI output.
    assert result.stdout == stdout.decode("utf-8")


@pytest.mark.asyncio
async def test_zero_exit_error_wrapper_names_the_subtype_and_preserves_process_stderr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The actionable annotation names the wrapper's `subtype` (e.g. error_max_turns)
    # so the failure is diagnosable from the trace, AND it preserves any stderr the
    # process already emitted by appending rather than clobbering (#9 review follow-up).
    schema = write_schema(tmp_path / "s.schema.json", {"type": "object"})
    stdout = claude_json_result(result="hit the cap", is_error=True, subtype="error_max_turns")
    patch_which(monkeypatch)
    patch_spawn(monkeypatch, FakeProcess(0, stdout=stdout, stderr=b"prior process noise"))
    adapter = ClaudePrintAdapter()

    result = await adapter.invoke(
        AgentInvocation(node_id="n", adapter="claude.print", prompt="p", output_schema=schema)
    )

    assert result.adapter_failure is True
    assert "error_max_turns" in result.stderr, "the annotation names the wrapper subtype"
    assert "prior process noise" in result.stderr, "existing process stderr is preserved"


@pytest.mark.asyncio
async def test_zero_exit_error_wrapper_stderr_ending_in_newline_has_no_blank_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The is_error path raises `adapter_failure` (#83), so the node is failed and the
    # executor's success-only `.strip()` never cleans the persisted stderr. When the
    # process stderr already ends in a newline, the annotation must NOT introduce a
    # doubled/trailing blank line — yet it must still name the subtype.
    schema = write_schema(tmp_path / "s.schema.json", {"type": "object"})
    stdout = claude_json_result(result="hit the cap", is_error=True, subtype="error_max_turns")
    patch_which(monkeypatch)
    patch_spawn(monkeypatch, FakeProcess(0, stdout=stdout, stderr=b"prior process noise\n"))
    adapter = ClaudePrintAdapter()

    result = await adapter.invoke(
        AgentInvocation(node_id="n", adapter="claude.print", prompt="p", output_schema=schema)
    )

    assert "error_max_turns" in result.stderr, "the annotation still names the wrapper subtype"
    assert "\n\n" not in result.stderr, "no blank line between the process stderr and annotation"
    assert result.stderr == (
        "prior process noise\nclaude reported an error (subtype: error_max_turns)"
    )


@pytest.mark.asyncio
async def test_capability_check_records_the_cli_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The capability check probes `claude --version` and records the version. It is
    # adapter infrastructure (not a node invocation), so it may use the ambient
    # environment to locate and run the CLI. The version is adapter-local — returned
    # here, never persisted to State (#79 carve-out).
    patch_which(monkeypatch)
    captured = patch_spawn(monkeypatch, FakeProcess(0, stdout=b"2.1.177 (Claude Code)\n"))
    adapter = ClaudePrintAdapter()

    version = await adapter.capability_check()

    assert version == "2.1.177 (Claude Code)"
    argv = captured["args"]
    assert isinstance(argv, tuple)
    assert argv == (FAKE_CLAUDE_PATH, "--version")
    # Ambient environment: the probe does not pass a filtered env=... allow-list,
    # so it can locate/run the CLI like any infrastructure command.
    assert captured["env"] is None


@pytest.mark.asyncio
async def test_capability_check_on_a_missing_cli_is_an_actionable_setup_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The capability check locates the CLI the same way invoke does: when
    # shutil.which returns None it raises the actionable setup AdapterError BEFORE
    # spawning the version probe.
    patch_which(monkeypatch, resolved=None)

    async def fail_if_spawned(*args: object, **kwargs: object) -> object:
        raise AssertionError("a missing CLI must error before create_subprocess_exec")

    monkeypatch.setattr("caw.claude_print.asyncio.create_subprocess_exec", fail_if_spawned)
    adapter = ClaudePrintAdapter()

    with pytest.raises(AdapterError) as excinfo:
        await adapter.capability_check()

    assert "install" in str(excinfo.value).lower()


def test_claude_print_is_a_builtin_adapter_name() -> None:
    # `caw validate` checks an agent node's adapter against BUILTIN_ADAPTER_NAMES
    # at normalize time, so the name must be registered for validation to accept it.
    from caw.adapter import BUILTIN_ADAPTER_NAMES

    assert "claude.print" in BUILTIN_ADAPTER_NAMES


def test_default_registry_resolves_exactly_the_builtin_adapter_names() -> None:
    # BUILTIN_ADAPTER_NAMES (the validate-time set) and _default_adapters (the
    # run-time registry) are two hand-maintained mirrors: a future adapter added to
    # one but not the other silently drifts the validate-time set from what a
    # default run actually resolves. Pin their agreement so that drift fails here.
    from caw.adapter import BUILTIN_ADAPTER_NAMES, AdapterRegistry

    assert AdapterRegistry().names == BUILTIN_ADAPTER_NAMES


def test_default_registry_resolves_claude_print_with_no_construction_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A default-registry run must resolve `claude.print`, AND constructing the
    # registry/adapter must NOT probe the CLI — so a shell-only or offline run
    # never requires `claude` to be installed. We assert no subprocess spawn fires
    # during construction or resolution.
    from caw.adapter import AdapterRegistry

    def fail_if_spawned(*args: object, **kwargs: object) -> object:
        raise AssertionError("constructing/resolving the adapter must not spawn a subprocess")

    monkeypatch.setattr("caw.claude_print.asyncio.create_subprocess_exec", fail_if_spawned)

    registry = AdapterRegistry()
    adapter = registry.resolve("claude.print")

    assert isinstance(adapter, ClaudePrintAdapter)
    assert "claude.print" in registry.names


def test_agent_node_with_claude_print_adapter_validates() -> None:
    # An agent node declaring `adapter: claude.print` passes normalize-time
    # validation (the #64 unknown-adapter check accepts the registered name).
    from caw.model import AgentNodeInputs, normalize_workflow

    raw = {
        "name": "sample",
        "version": 1,
        "nodes": [
            {
                "id": "agent",
                "kind": "agent",
                "inputs": {"adapter": "claude.print", "prompt": "summarize the repo"},
            }
        ],
    }

    workflow = normalize_workflow(raw, source="<test>")

    (node,) = workflow.nodes
    assert isinstance(node.inputs, AgentNodeInputs)
    assert node.inputs.adapter == "claude.print"


@pytest.mark.asyncio
async def test_claude_print_node_runs_end_to_end_through_the_default_registry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The full offline path: an `agent` node with `adapter: claude.print` and an
    # `output_schema`, run through `execute_run` with the DEFAULT AdapterRegistry()
    # (no wiring). The CLI lookup and the subprocess spawn are the only seams — both
    # monkeypatched — so no real `claude` is needed. This proves path node -> default
    # registry -> claude.print adapter -> Output Contract -> State works end to end:
    # the structured output the (fake) CLI emitted is validated against the schema and
    # persisted to the node's State output.
    schema = write_schema(
        tmp_path / "person.schema.json",
        {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
    )
    stdout = claude_json_result(result="made a person", structured_output={"name": "Alice"})
    patch_which(monkeypatch)
    patch_spawn(monkeypatch, FakeProcess(0, stdout=stdout))
    raw = {
        "name": "sample",
        "version": 1,
        "nodes": [
            {
                "id": "make_person",
                "kind": "agent",
                "inputs": {
                    "adapter": "claude.print",
                    "prompt": "make a person",
                    "output_schema": str(schema),
                },
            }
        ],
    }
    workflow = normalize_workflow(raw, source="<test>")

    result = await execute_run(workflow, tmp_path / "runs", registry=AdapterRegistry())

    assert result.succeeded, "the claude.print node ran and satisfied its Output Contract"
    (run_dir,) = (tmp_path / "runs").iterdir()
    connection = sqlite3.connect(run_dir / "state.sqlite")
    try:
        (row,) = connection.execute(
            "SELECT output_json FROM attempt WHERE node_id = 'make_person'"
        ).fetchall()
    finally:
        connection.close()
    persisted = json.loads(row[0])
    assert persisted["structured_output"] == {"name": "Alice"}, (
        "the node's persisted State output carries the adapter's structured output"
    )
