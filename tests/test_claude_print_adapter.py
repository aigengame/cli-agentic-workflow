"""Offline tests for the real ``claude.print`` Adapter (#9).

These prove the Adapter normalizes ``claude -p`` invocations into vendor-neutral
:class:`AgentResult`s and reports an actionable setup error for a missing CLI,
WITHOUT a real ``claude`` on PATH: the subprocess spawn and the version probe are
the only seams, and they are monkeypatched here. A separate online test
(``test_claude_print_real_cli``) exercises a real ``claude`` and auto-skips when
the CLI is absent.
"""

import json
import shutil
from pathlib import Path

import pytest

from caw.adapter import AdapterError, AgentInvocation
from caw.claude_print import ClaudePrintAdapter


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


def write_schema(path: Path, schema: dict[str, object]) -> Path:
    path.write_text(json.dumps(schema), encoding="utf-8")
    return path


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


@pytest.mark.asyncio
async def test_prompt_is_positional_and_node_args_pass_through_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The prompt is a positional argument to `claude -p`; the node's `args`
    # (sandbox/approval/any other CLI flags) pass through to the subprocess
    # UNCHANGED. caw adds no policy engine: it neither interprets nor injects flags.
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
        "claude",
        "-p",
        "summarize the repo",
        "--permission-mode",
        "acceptEdits",
        "--add-dir",
        "/tmp/x",
    )


@pytest.mark.asyncio
async def test_only_the_invocation_env_reaches_the_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Env policy (ADR 0006, #5): the executor hands the adapter an already-filtered
    # allow-list. The adapter passes EXACTLY that to the subprocess — never merging
    # the parent os.environ, which would leak the whole environment.
    monkeypatch.setenv("PARENT_ONLY_VAR", "leaked-if-present")
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
    assert result.exit_status == 0
    assert result.structured_output == {"name": "Alice", "age": 30}


@pytest.mark.asyncio
async def test_no_output_schema_does_not_request_json_and_leaves_structured_output_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Without an output_schema the adapter does NOT request JSON output: stdout is
    # the CLI's raw text and structured_output stays None.
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
    patch_spawn(monkeypatch, FakeProcess(2, stdout=b"error text, not json", stderr=b"boom"))
    adapter = ClaudePrintAdapter()

    result = await adapter.invoke(
        AgentInvocation(node_id="n", adapter="claude.print", prompt="p", output_schema=schema)
    )

    assert result.exit_status == 2
    assert result.structured_output is None
    assert result.stderr == "boom"


@pytest.mark.asyncio
async def test_capability_check_records_the_cli_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The capability check probes `claude --version` and records the version. It is
    # adapter infrastructure (not a node invocation), so it may use the ambient
    # environment to locate and run the CLI. The version is adapter-local — returned
    # here, never persisted to State (#79 carve-out).
    captured = patch_spawn(monkeypatch, FakeProcess(0, stdout=b"2.1.177 (Claude Code)\n"))
    adapter = ClaudePrintAdapter()

    version = await adapter.capability_check()

    assert version == "2.1.177 (Claude Code)"
    argv = captured["args"]
    assert isinstance(argv, tuple)
    assert argv == ("claude", "--version")
    # Ambient environment: the probe does not pass a filtered env=... allow-list,
    # so it can locate/run the CLI like any infrastructure command.
    assert captured["env"] is None


@pytest.mark.asyncio
async def test_capability_check_on_a_missing_cli_is_an_actionable_setup_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def raise_not_found(*args: object, **kwargs: object) -> object:
        raise FileNotFoundError(2, "No such file or directory", "claude")

    monkeypatch.setattr("caw.claude_print.asyncio.create_subprocess_exec", raise_not_found)
    adapter = ClaudePrintAdapter()

    with pytest.raises(AdapterError) as excinfo:
        await adapter.capability_check()

    assert "install" in str(excinfo.value).lower()


def test_claude_print_is_a_builtin_adapter_name() -> None:
    # `caw validate` checks an agent node's adapter against BUILTIN_ADAPTER_NAMES
    # at normalize time, so the name must be registered for validation to accept it.
    from caw.adapter import BUILTIN_ADAPTER_NAMES

    assert "claude.print" in BUILTIN_ADAPTER_NAMES


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


@pytest.mark.skipif(shutil.which("claude") is None, reason="the 'claude' CLI is not installed")
@pytest.mark.asyncio
async def test_claude_print_real_cli_capability_check() -> None:
    # The ONLY test that spawns a real `claude`. It auto-skips when the CLI is
    # absent (the offline suite above is what proves the acceptance criteria). It
    # exercises the version PROBE — offline, free, deterministic, and needing no
    # auth — to prove the real-CLI capability-check path works end to end.
    adapter = ClaudePrintAdapter()

    version = await adapter.capability_check()

    assert version, "a real `claude --version` reports a non-empty version string"
