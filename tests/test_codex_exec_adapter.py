"""Offline tests for the real ``codex.exec`` Adapter (#11).

These prove the Adapter normalizes ``codex exec`` invocations into vendor-neutral
:class:`AgentResult`s and reports an actionable setup error for a missing CLI,
WITHOUT a real ``codex`` on PATH: the subprocess spawn and the version probe are
the only seams, and they are monkeypatched here. Real-CLI coverage lives in the
``e2e`` tier (``tests/e2e/``), which runs a real ``codex`` locally and FAILS — never
skips — when the CLI is unavailable (#86).

The structure deliberately mirrors ``tests/test_claude_print_adapter.py``: the two
Adapters are capability-symmetric (#11 acceptance), so a node switches between them
by changing ONLY its adapter name. The codex-specific knowledge — ``codex exec``,
``--json`` JSONL events, ``--output-schema``, the ``agent_message`` item and the
``turn.failed`` event — lives in the adapter and is asserted here, never in the
kernel.
"""

import asyncio
import json
import signal
import sqlite3
from pathlib import Path

import pytest
from conftest import write_schema

from caw.adapter import AdapterError, AdapterRegistry, AgentInvocation
from caw.codex_exec import CodexExecAdapter
from caw.executor import execute_run
from caw.model import normalize_workflow


def codex_jsonl(*, text: str | None = "ok", failed: str | None = None) -> bytes:
    """Build the JSONL event stream ``codex exec --json`` prints.

    Mirrors the real CLI shape: a ``thread.started`` then ``turn.started``, then —
    on success — an ``item.completed`` carrying an ``agent_message`` item whose
    ``text`` is the agent's final message (the structured-output JSON string when an
    ``--output-schema`` was supplied), then ``turn.completed`` with token ``usage``.
    On failure a ``turn.failed`` event carries the error ``message`` instead. Encoded
    to bytes as the subprocess would emit it.
    """
    events: list[dict[str, object]] = [
        {"type": "thread.started", "thread_id": "abc123"},
        {"type": "turn.started"},
    ]
    if failed is not None:
        events.append({"type": "turn.failed", "error": {"message": failed}})
    else:
        if text is not None:
            events.append(
                {
                    "type": "item.completed",
                    "item": {"id": "item_0", "type": "agent_message", "text": text},
                }
            )
        # TODO(#79): usage — codex surfaces token counts here; #79 owns the usage
        # contract and will plumb it into AgentResult/State for BOTH adapters.
        events.append(
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 10, "output_tokens": 2},
            }
        )
    return ("\n".join(json.dumps(event) for event in events) + "\n").encode("utf-8")


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
        self.waited = True
        if self.returncode is None:
            self.returncode = -9
        return self.returncode


class ProducingProcess(FakeProcess):
    """A fake CLI process that writes one artifact while it runs."""

    def __init__(self, artifact: Path) -> None:
        super().__init__(0, stdout=codex_jsonl(text="ok"))
        self._artifact = artifact

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        self._artifact.write_text("created by codex\n", encoding="utf-8")
        return await super().communicate(input)


FAKE_CODEX_PATH = "/fake/abs/bin/codex"


def patch_killpg(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int]]:
    """Patch ``os.killpg`` in the shared subprocess-adapter namespace (#83).

    The process-group lifecycle moved out of ``codex.exec`` into the shared
    ``caw.subprocess_adapter`` base, so the kill seam now lives there; the offline
    suite patches it at its real home. Record (pid, signal) calls.
    """
    calls: list[tuple[int, int]] = []

    def fake_killpg(pid: int, sig: int) -> None:
        calls.append((pid, sig))

    monkeypatch.setattr("caw.subprocess_adapter.os.killpg", fake_killpg)
    return calls


def patch_spawn(monkeypatch: pytest.MonkeyPatch, process: FakeProcess) -> dict[str, object]:
    """Patch ``asyncio.create_subprocess_exec`` (in the shared base) to return ``process``.

    Records args/env. The spawn seam lives in ``caw.subprocess_adapter`` since #83
    consolidated the subprocess machinery there.
    """
    captured: dict[str, object] = {}

    async def fake_exec(*args: object, **kwargs: object) -> FakeProcess:
        captured["args"] = args
        captured["env"] = kwargs.get("env")
        captured["kwargs"] = kwargs
        return process

    monkeypatch.setattr("caw.subprocess_adapter.asyncio.create_subprocess_exec", fake_exec)
    return captured


def patch_which(monkeypatch: pytest.MonkeyPatch, resolved: str | None = FAKE_CODEX_PATH) -> None:
    """Patch ``shutil.which`` (in the shared subprocess-adapter namespace) to ``resolved``.

    Locating the CLI moved to the shared base (#83), so it is patched there.
    ``resolved=None`` models a missing CLI.
    """
    monkeypatch.setattr("caw.subprocess_adapter.shutil.which", lambda _name: resolved)


@pytest.mark.asyncio
async def test_zero_exit_normalizes_agent_message_and_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_which(monkeypatch)
    patch_spawn(
        monkeypatch,
        FakeProcess(0, stdout=codex_jsonl(text="a one-line summary"), stderr=b""),
    )
    adapter = CodexExecAdapter()

    result = await adapter.invoke(
        AgentInvocation(node_id="n", adapter="codex.exec", prompt="summarize")
    )

    assert result.exit_status == 0
    # stdout is the agent's final message text, extracted from the JSONL events —
    # the vendor-neutral analogue of claude.print's freeform stdout text.
    assert result.stdout == "a one-line summary"
    assert result.stderr == ""
    assert result.structured_output is None
    assert result.artifacts == ()


@pytest.mark.asyncio
async def test_invoke_reports_files_created_by_the_cli_as_artifacts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # #16: codex.exec has the same artifact-capture behavior as claude.print.
    produced = tmp_path / "agent-report.md"
    monkeypatch.chdir(tmp_path)
    patch_which(monkeypatch)
    patch_spawn(monkeypatch, ProducingProcess(produced))
    adapter = CodexExecAdapter()

    result = await adapter.invoke(
        AgentInvocation(node_id="n", adapter="codex.exec", prompt="write a report")
    )

    assert result.exit_status == 0
    assert result.artifacts == (produced,)


@pytest.mark.asyncio
async def test_non_zero_exit_is_an_ordinary_result_not_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ADR 0006: a `codex` process that RAN and exited non-zero is a normal
    # AgentResult(exit_status=N), never an AdapterError. AdapterError is reserved
    # for the Adapter being unable to produce a result at all.
    patch_which(monkeypatch)
    patch_spawn(monkeypatch, FakeProcess(2, stdout=b"", stderr=b"error: bad flag"))
    adapter = CodexExecAdapter()

    result = await adapter.invoke(
        AgentInvocation(node_id="n", adapter="codex.exec", prompt="do it")
    )

    assert result.exit_status == 2
    assert result.stderr == "error: bad flag"
    assert result.structured_output is None


@pytest.mark.asyncio
async def test_invalid_utf8_bytes_decode_recoverably_not_with_replacement_chars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # stderr feeds State and downstream `when` predicates, so an undecodable byte must
    # decode RECOVERABLY (backslashreplace -> `\xff`), like the executor's shell node,
    # not irreversibly (replace -> U+FFFD which loses the original byte). A non-zero
    # exit takes the raw-stdout path (no JSONL to parse), so undecodable stdout bytes
    # are exercised here too.
    patch_which(monkeypatch)
    patch_spawn(monkeypatch, FakeProcess(3, stdout=b"out\xff", stderr=b"err\xfe"))
    adapter = CodexExecAdapter()

    result = await adapter.invoke(
        AgentInvocation(node_id="n", adapter="codex.exec", prompt="do it")
    )

    assert "�" not in result.stdout and "�" not in result.stderr, "no lossy U+FFFD"
    assert result.stdout == "out\\xff"
    assert result.stderr == "err\\xfe"


@pytest.mark.asyncio
async def test_missing_cli_raises_an_actionable_setup_error_before_spawning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # When `codex` is not on PATH (shutil.which returns None), invoke raises an
    # ACTIONABLE AdapterError (a setup message telling the user how to install/enable
    # it) BEFORE attempting to spawn — separating locate-the-tool from run-the-tool.
    patch_which(monkeypatch, resolved=None)

    async def fail_if_spawned(*args: object, **kwargs: object) -> object:
        raise AssertionError("a missing CLI must error before create_subprocess_exec")

    monkeypatch.setattr("caw.subprocess_adapter.asyncio.create_subprocess_exec", fail_if_spawned)
    adapter = CodexExecAdapter()

    with pytest.raises(AdapterError) as excinfo:
        await adapter.invoke(AgentInvocation(node_id="n", adapter="codex.exec", prompt="do it"))

    message = str(excinfo.value)
    assert "codex" in message
    assert "install" in message.lower(), "the error tells the user how to install/enable the CLI"


@pytest.mark.asyncio
async def test_invoke_spawns_in_its_own_session_with_isolated_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The spawn must start a new session (so the whole process tree can be signalled
    # by process group on cancellation/timeout) and isolate stdin (DEVNULL) so the
    # child cannot block reading the parent's stdin. codex exec reads supplementary
    # input from stdin, so DEVNULL is load-bearing: it stops a real `codex exec` from
    # blocking forever waiting for piped input.
    patch_which(monkeypatch)
    captured = patch_spawn(monkeypatch, FakeProcess(0, stdout=codex_jsonl(text="ok")))
    adapter = CodexExecAdapter()

    await adapter.invoke(AgentInvocation(node_id="n", adapter="codex.exec", prompt="p"))

    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs.get("start_new_session") is True
    assert kwargs.get("stdin") == asyncio.subprocess.DEVNULL


@pytest.mark.asyncio
async def test_capability_check_spawns_in_its_own_session_with_isolated_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_which(monkeypatch)
    captured = patch_spawn(monkeypatch, FakeProcess(0, stdout=b"codex-cli 0.137.0\n"))
    adapter = CodexExecAdapter()

    await adapter.capability_check()

    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs.get("start_new_session") is True
    assert kwargs.get("stdin") == asyncio.subprocess.DEVNULL


@pytest.mark.asyncio
async def test_invoke_kills_and_reaps_the_subprocess_tree_on_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_which(monkeypatch)
    killed = patch_killpg(monkeypatch)
    process = FakeProcess(None, communicate_raises=asyncio.CancelledError())
    patch_spawn(monkeypatch, process)
    adapter = CodexExecAdapter()

    with pytest.raises(asyncio.CancelledError):
        await adapter.invoke(AgentInvocation(node_id="n", adapter="codex.exec", prompt="p"))

    assert killed == [(process.pid, signal.SIGKILL)], "the process tree is killed by group"
    assert process.waited is True, "the killed process is reaped (no orphan)"


@pytest.mark.asyncio
async def test_invoke_kills_and_reaps_on_timeout_error_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_which(monkeypatch)
    killed = patch_killpg(monkeypatch)
    process = FakeProcess(None, communicate_raises=TimeoutError())
    patch_spawn(monkeypatch, process)
    adapter = CodexExecAdapter()

    with pytest.raises(TimeoutError):
        await adapter.invoke(AgentInvocation(node_id="n", adapter="codex.exec", prompt="p"))

    assert killed and process.waited is True


@pytest.mark.asyncio
async def test_invoke_cancellation_kills_group_even_if_leader_already_exited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_which(monkeypatch)
    killed = patch_killpg(monkeypatch)
    process = FakeProcess(0, communicate_raises=asyncio.CancelledError())
    patch_spawn(monkeypatch, process)
    adapter = CodexExecAdapter()

    with pytest.raises(asyncio.CancelledError):
        await adapter.invoke(AgentInvocation(node_id="n", adapter="codex.exec", prompt="p"))

    assert killed == [(process.pid, signal.SIGKILL)], (
        "the group is killed even though the leader already exited (a grandchild may survive)"
    )
    assert process.waited is True, "it is still reaped"


@pytest.mark.asyncio
async def test_invoke_cancellation_suppresses_process_lookup_when_group_is_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_which(monkeypatch)

    def killpg_no_such_group(pid: int, sig: int) -> None:
        raise ProcessLookupError(3, "No such process")

    monkeypatch.setattr("caw.subprocess_adapter.os.killpg", killpg_no_such_group)
    process = FakeProcess(None, communicate_raises=asyncio.CancelledError())
    patch_spawn(monkeypatch, process)
    adapter = CodexExecAdapter()

    with pytest.raises(asyncio.CancelledError):
        await adapter.invoke(AgentInvocation(node_id="n", adapter="codex.exec", prompt="p"))

    assert process.waited is True, "the process is still reaped after the suppressed lookup error"


@pytest.mark.asyncio
async def test_capability_check_kills_and_reaps_the_subprocess_on_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_which(monkeypatch)
    killed = patch_killpg(monkeypatch)
    process = FakeProcess(None, communicate_raises=asyncio.CancelledError())
    patch_spawn(monkeypatch, process)
    adapter = CodexExecAdapter()

    with pytest.raises(asyncio.CancelledError):
        await adapter.capability_check()

    assert killed == [(process.pid, signal.SIGKILL)]
    assert process.waited is True


@pytest.mark.asyncio
async def test_filenotfound_at_spawn_is_translated_as_a_toctou_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_which(monkeypatch)

    async def raise_not_found(*args: object, **kwargs: object) -> object:
        raise FileNotFoundError(2, "No such file or directory", FAKE_CODEX_PATH)

    monkeypatch.setattr("caw.subprocess_adapter.asyncio.create_subprocess_exec", raise_not_found)
    adapter = CodexExecAdapter()

    with pytest.raises(AdapterError) as excinfo:
        await adapter.invoke(AgentInvocation(node_id="n", adapter="codex.exec", prompt="do it"))

    message = str(excinfo.value)
    assert "codex" in message
    assert "install" in message.lower(), "the error tells the user how to install/enable the CLI"


@pytest.mark.asyncio
async def test_invoke_spawns_the_resolved_absolute_cli_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Locating the tool is infrastructure (use the ambient PATH), separate from the
    # child's env policy. invoke resolves `codex` to an ABSOLUTE path via shutil.which
    # and spawns THAT path, so OS executable lookup never depends on a PATH in the
    # node's env allow-list. The exec subcommand and `--json` precede the prompt; the
    # prompt is last, after the `--` separator.
    patch_which(monkeypatch)
    captured = patch_spawn(monkeypatch, FakeProcess(0, stdout=codex_jsonl(text="ok")))
    adapter = CodexExecAdapter()

    await adapter.invoke(AgentInvocation(node_id="n", adapter="codex.exec", prompt="summarize"))

    argv = captured["args"]
    assert isinstance(argv, tuple)
    assert argv[0] == FAKE_CODEX_PATH, "argv[0] is the which-resolved absolute path"
    assert argv[1] == "exec", "the headless exec subcommand"
    assert "--json" in argv, "structured JSONL events are requested"
    assert argv[-1] == "summarize", "the prompt is the last argv element"
    assert "--" in argv and argv[argv.index("--") + 1] == "summarize", (
        "the prompt is the trailing positional, after the `--` separator"
    )


@pytest.mark.asyncio
async def test_prompt_is_positional_and_node_args_pass_through_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The prompt is the trailing positional to `codex exec`, after a `--` end-of-options
    # separator; the node's `args` (sandbox/approval/any other CLI flags) pass through
    # UNCHANGED, BEFORE the `--`. caw adds no policy engine: it neither interprets nor
    # injects flags. Sandbox/approval flags are exactly such passthrough `args` (#11
    # acceptance 3).
    patch_which(monkeypatch)
    captured = patch_spawn(monkeypatch, FakeProcess(0, stdout=codex_jsonl(text="ok")))
    adapter = CodexExecAdapter()

    await adapter.invoke(
        AgentInvocation(
            node_id="n",
            adapter="codex.exec",
            prompt="summarize the repo",
            args=("--sandbox", "read-only", "--add-dir", "/tmp/x"),
        )
    )

    argv = captured["args"]
    assert isinstance(argv, tuple)
    # The passthrough args appear verbatim, before the `--` separator and the prompt.
    assert "--sandbox" in argv and argv[argv.index("--sandbox") + 1] == "read-only"
    assert "--add-dir" in argv and argv[argv.index("--add-dir") + 1] == "/tmp/x"
    separator = argv.index("--")
    assert argv.index("--sandbox") < separator, "passthrough flags precede the separator"
    assert argv[separator + 1] == "summarize the repo", "the prompt follows the separator"


@pytest.mark.asyncio
async def test_leading_dash_prompt_is_protected_by_the_end_of_options_separator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A prompt that begins with `-` (e.g. "--help") must NEVER be parsed by `codex exec`
    # as a flag. The `--` end-of-options separator guarantees the prompt is treated as a
    # positional: `--` is present and the prompt is the LAST argv element.
    patch_which(monkeypatch)
    captured = patch_spawn(monkeypatch, FakeProcess(0, stdout=codex_jsonl(text="ok")))
    adapter = CodexExecAdapter()

    await adapter.invoke(AgentInvocation(node_id="n", adapter="codex.exec", prompt="--help"))

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
    # allow-list. The adapter passes EXACTLY that to the subprocess — never merging the
    # parent os.environ. The node env LACKS PATH, yet invoke still LOCATES the CLI
    # because shutil.which uses the ambient env (infrastructure) — separate from the
    # child's env policy. The child receives EXACTLY {"DECLARED_VAR": "..."} and no PATH.
    monkeypatch.setenv("PARENT_ONLY_VAR", "leaked-if-present")
    patch_which(monkeypatch)
    captured = patch_spawn(monkeypatch, FakeProcess(0, stdout=codex_jsonl(text="ok")))
    adapter = CodexExecAdapter()

    await adapter.invoke(
        AgentInvocation(
            node_id="n",
            adapter="codex.exec",
            prompt="do it",
            env={"DECLARED_VAR": "declared-value"},
        )
    )

    assert captured["env"] == {"DECLARED_VAR": "declared-value"}, (
        "exactly the invocation env reaches the subprocess, with no parent-env leakage"
    )
    assert "PATH" not in captured["env"], "locating the CLI does not inject PATH into the child"


@pytest.mark.asyncio
async def test_output_schema_requests_json_schema_and_parses_structured_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # When the node declares an output_schema, the adapter passes it to the CLI
    # (`--output-schema <path>`) and parses the agent_message text (a JSON string) into
    # AgentResult.structured_output. The KERNEL validates the schema afterward; the
    # adapter does NOT validate it.
    schema = write_schema(
        tmp_path / "person.schema.json",
        {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
            "additionalProperties": False,
        },
    )
    stdout = codex_jsonl(text=json.dumps({"name": "Alice", "age": 30}))
    patch_which(monkeypatch)
    captured = patch_spawn(monkeypatch, FakeProcess(0, stdout=stdout))
    adapter = CodexExecAdapter()

    result = await adapter.invoke(
        AgentInvocation(
            node_id="n", adapter="codex.exec", prompt="make a person", output_schema=schema
        )
    )

    argv = captured["args"]
    assert isinstance(argv, tuple)
    # codex takes the schema as a FILE PATH (unlike claude's inline schema text).
    assert "--output-schema" in argv and argv[argv.index("--output-schema") + 1] == str(schema)
    assert result.exit_status == 0
    assert result.structured_output == {"name": "Alice", "age": 30}


@pytest.mark.asyncio
async def test_no_output_schema_does_not_request_a_schema_and_leaves_structured_output_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Without an output_schema the adapter does NOT pass --output-schema: stdout is the
    # agent's final message text and structured_output stays None.
    patch_which(monkeypatch)
    captured = patch_spawn(
        monkeypatch, FakeProcess(0, stdout=codex_jsonl(text="plain text answer"))
    )
    adapter = CodexExecAdapter()

    result = await adapter.invoke(
        AgentInvocation(node_id="n", adapter="codex.exec", prompt="answer")
    )

    argv = captured["args"]
    assert isinstance(argv, tuple)
    assert "--output-schema" not in argv
    assert result.stdout == "plain text answer"
    assert result.structured_output is None


@pytest.mark.asyncio
async def test_unparseable_structured_text_when_a_schema_was_required_is_an_adapter_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # When the node required structured output but the agent_message text is not the
    # expected JSON, the adapter cannot produce a result: ADR 0006 reserves AdapterError
    # for exactly this (output required, unparseable).
    schema = write_schema(tmp_path / "s.schema.json", {"type": "object"})
    patch_which(monkeypatch)
    patch_spawn(monkeypatch, FakeProcess(0, stdout=codex_jsonl(text="not json at all")))
    adapter = CodexExecAdapter()

    with pytest.raises(AdapterError) as excinfo:
        await adapter.invoke(
            AgentInvocation(node_id="n", adapter="codex.exec", prompt="p", output_schema=schema)
        )

    assert "n" in str(excinfo.value)


@pytest.mark.asyncio
async def test_absent_agent_message_when_a_schema_was_required_is_an_adapter_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A zero-exit structured run whose event stream carries NO agent_message means the
    # CLI was asked for structured output but produced none. The adapter cannot produce
    # a result — an AdapterError, consistent with the unparseable-when-required case.
    schema = write_schema(tmp_path / "s.schema.json", {"type": "object"})
    patch_which(monkeypatch)
    patch_spawn(monkeypatch, FakeProcess(0, stdout=codex_jsonl(text=None)))
    adapter = CodexExecAdapter()

    with pytest.raises(AdapterError) as excinfo:
        await adapter.invoke(
            AgentInvocation(node_id="n", adapter="codex.exec", prompt="p", output_schema=schema)
        )

    message = str(excinfo.value)
    assert "n" in message
    assert "structured" in message.lower() or "message" in message.lower()


@pytest.mark.asyncio
async def test_non_zero_exit_with_schema_does_not_attempt_to_parse_structured_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A non-zero exit is an ordinary AgentResult even when a schema was declared: the
    # adapter must NOT raise an AdapterError for unparseable output — the process failed
    # and its exit status is the node's failure, parseability moot.
    schema = write_schema(tmp_path / "s.schema.json", {"type": "object"})
    patch_which(monkeypatch)
    patch_spawn(monkeypatch, FakeProcess(2, stdout=b"error text, not json", stderr=b"boom"))
    adapter = CodexExecAdapter()

    result = await adapter.invoke(
        AgentInvocation(node_id="n", adapter="codex.exec", prompt="p", output_schema=schema)
    )

    assert result.exit_status == 2
    assert result.structured_output is None
    assert result.stderr == "boom"


@pytest.mark.asyncio
async def test_zero_exit_but_turn_failed_event_normalizes_to_a_failed_node(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Defense-in-depth, symmetric with claude.print's is_error handling: `codex` can
    # emit a `turn.failed` event in its JSONL stream even on a ZERO process exit. On the
    # structured path the adapter inspects the events: exit 0 + a turn.failed event is
    # normalized to a FAILED node via the FIRST-CLASS `adapter_failure` signal (#84) —
    # the adapter KEEPS the process's real exit_status (here 0) rather than fabricating a
    # non-zero exit, raises `adapter_failure`, drops the structured_output (a failed node
    # carries no trustworthy output), and appends an actionable annotation carrying the
    # codex error message to stderr.
    schema = write_schema(tmp_path / "s.schema.json", {"type": "object"})
    stdout = codex_jsonl(failed="model overloaded, try again")
    patch_which(monkeypatch)
    patch_spawn(monkeypatch, FakeProcess(0, stdout=stdout))
    adapter = CodexExecAdapter()

    result = await adapter.invoke(
        AgentInvocation(node_id="n", adapter="codex.exec", prompt="p", output_schema=schema)
    )

    assert result.exit_status == 0, "the real process exit_status is preserved, not fabricated"
    assert result.adapter_failure is True, "the failure rides the first-class signal"
    assert result.structured_output is None
    assert "error" in result.stderr.lower(), "stderr carries an actionable CLI-error annotation"
    assert "overloaded" in result.stderr, "the codex error message is surfaced for diagnosis"


@pytest.mark.asyncio
async def test_capability_check_records_the_cli_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The capability check probes `codex --version` and records the version. It is
    # adapter infrastructure (not a node invocation), so it may use the ambient
    # environment to locate and run the CLI. The version is adapter-local — returned
    # here, never persisted to State (#79 carve-out).
    patch_which(monkeypatch)
    captured = patch_spawn(monkeypatch, FakeProcess(0, stdout=b"codex-cli 0.137.0\n"))
    adapter = CodexExecAdapter()

    version = await adapter.capability_check()

    assert version == "codex-cli 0.137.0"
    argv = captured["args"]
    assert isinstance(argv, tuple)
    assert argv == (FAKE_CODEX_PATH, "--version")
    # Ambient environment: the probe does not pass a filtered env=... allow-list.
    assert captured["env"] is None


@pytest.mark.asyncio
async def test_capability_check_on_a_missing_cli_is_an_actionable_setup_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_which(monkeypatch, resolved=None)

    async def fail_if_spawned(*args: object, **kwargs: object) -> object:
        raise AssertionError("a missing CLI must error before create_subprocess_exec")

    monkeypatch.setattr("caw.subprocess_adapter.asyncio.create_subprocess_exec", fail_if_spawned)
    adapter = CodexExecAdapter()

    with pytest.raises(AdapterError) as excinfo:
        await adapter.capability_check()

    assert "install" in str(excinfo.value).lower()


def test_codex_exec_is_a_builtin_adapter_name() -> None:
    # `caw validate` checks an agent node's adapter against BUILTIN_ADAPTER_NAMES at
    # normalize time, so the name must be registered for validation to accept it.
    from caw.adapter import BUILTIN_ADAPTER_NAMES

    assert "codex.exec" in BUILTIN_ADAPTER_NAMES


def test_default_registry_resolves_codex_exec_with_no_construction_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A default-registry run must resolve `codex.exec`, AND constructing the
    # registry/adapter must NOT probe the CLI — so a shell-only or offline run never
    # requires `codex` to be installed. We assert no subprocess spawn fires during
    # construction or resolution.
    def fail_if_spawned(*args: object, **kwargs: object) -> object:
        raise AssertionError("constructing/resolving the adapter must not spawn a subprocess")

    monkeypatch.setattr("caw.subprocess_adapter.asyncio.create_subprocess_exec", fail_if_spawned)

    registry = AdapterRegistry()
    adapter = registry.resolve("codex.exec")

    assert isinstance(adapter, CodexExecAdapter)
    assert "codex.exec" in registry.names


def test_agent_node_with_codex_exec_adapter_validates() -> None:
    # An agent node declaring `adapter: codex.exec` passes normalize-time validation
    # (the #64 unknown-adapter check accepts the registered name).
    from caw.model import AgentNodeInputs

    raw = {
        "name": "sample",
        "version": 1,
        "nodes": [
            {
                "id": "agent",
                "kind": "agent",
                "inputs": {"adapter": "codex.exec", "prompt": "summarize the repo"},
            }
        ],
    }

    workflow = normalize_workflow(raw, source="<test>")

    (node,) = workflow.nodes
    assert isinstance(node.inputs, AgentNodeInputs)
    assert node.inputs.adapter == "codex.exec"


def test_claude_print_and_codex_exec_are_symmetric_only_the_adapter_name_changes() -> None:
    # #11 headline acceptance (symmetry): switching a node between `claude.print` and
    # `codex.exec` requires changing ONLY the adapter name. Build the SAME node twice,
    # differing in exactly one field — the adapter name — and both validate.
    from caw.model import AgentNodeInputs

    def one_node_workflow(adapter: str) -> dict[str, object]:
        return {
            "name": "sample",
            "version": 1,
            "nodes": [
                {
                    "id": "agent",
                    "kind": "agent",
                    "inputs": {
                        "adapter": adapter,
                        "prompt": "summarize the repo",
                        "output_schema": None,
                    },
                }
            ],
        }

    claude_raw = one_node_workflow("claude.print")
    codex_raw = one_node_workflow("codex.exec")
    # The two raw definitions differ in EXACTLY one leaf: inputs.adapter.
    assert claude_raw["nodes"][0]["inputs"]["adapter"] == "claude.print"  # type: ignore[index]
    assert codex_raw["nodes"][0]["inputs"]["adapter"] == "codex.exec"  # type: ignore[index]

    claude_wf = normalize_workflow(claude_raw, source="<test>")
    codex_wf = normalize_workflow(codex_raw, source="<test>")

    (claude_node,) = claude_wf.nodes
    (codex_node,) = codex_wf.nodes
    assert isinstance(claude_node.inputs, AgentNodeInputs)
    assert isinstance(codex_node.inputs, AgentNodeInputs)
    assert claude_node.inputs.adapter == "claude.print"
    assert codex_node.inputs.adapter == "codex.exec"
    # Every OTHER node attribute is identical — the swap touched only the adapter name.
    assert claude_node.inputs.prompt == codex_node.inputs.prompt
    assert claude_node.id == codex_node.id
    assert claude_node.kind == codex_node.kind


@pytest.mark.asyncio
async def test_codex_exec_node_runs_end_to_end_through_the_default_registry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The full offline path: an `agent` node with `adapter: codex.exec` and an
    # `output_schema`, run through `execute_run` with the DEFAULT AdapterRegistry()
    # (no wiring). The CLI lookup and subprocess spawn are the only seams — both
    # monkeypatched — so no real `codex` is needed. This proves path node -> default
    # registry -> codex.exec adapter -> Output Contract -> State works end to end.
    schema = write_schema(
        tmp_path / "person.schema.json",
        {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
            "additionalProperties": False,
        },
    )
    stdout = codex_jsonl(text=json.dumps({"name": "Alice"}))
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
                    "adapter": "codex.exec",
                    "prompt": "make a person",
                    "output_schema": str(schema),
                },
            }
        ],
    }
    workflow = normalize_workflow(raw, source="<test>")

    result = await execute_run(workflow, tmp_path / "runs", registry=AdapterRegistry())

    assert result.succeeded, "the codex.exec node ran and satisfied its Output Contract"
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
