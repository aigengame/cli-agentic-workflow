"""The ``claude.print`` Adapter: invokes ``claude -p`` headless mode (#9).

This is the first real Adapter behind the vendor-neutral interface of ADR 0006:
it spawns Claude Code's print/headless mode (``claude -p``) and normalizes the
process into an :class:`AgentResult`. ALL ``claude``-specific knowledge — the
``-p`` flag, ``--output-format json``, ``--json-schema``, and the result-wrapper
shape — lives here and nowhere in the kernel, so the executor, State, and Events
stay vendor-neutral.
"""

import asyncio
import contextlib
import json
import os
import shutil
import signal

from caw.adapter import Adapter, AdapterError, AgentInvocation, AgentResult

# The CLI entrypoint this Adapter drives. Resolved to an absolute path with
# shutil.which at invoke / capability-check time (lazily), never at construction,
# so a shell-only or offline Run never requires `claude`. Locating the binary uses
# the ambient PATH (infrastructure); it is deliberately NOT part of a node's env
# allow-list, so the child still receives only invocation.env (see _resolve_cli_path).
CLAUDE_CLI = "claude"

# The actionable setup message a missing CLI surfaces. ADR 0006 reserves
# AdapterError for the Adapter being unable to produce a result at all; a CLI that
# is not installed is exactly that, so the message tells the user how to install or
# enable it rather than leaking a raw FileNotFoundError.
_MISSING_CLI_HINT = (
    "the 'claude' CLI was not found on PATH. Install Claude Code "
    "(https://docs.claude.com/en/docs/claude-code/setup) and ensure 'claude' is on PATH, "
    "or run this node through the 'mock' adapter offline."
)


def _node_context(invocation: AgentInvocation) -> str:
    """The `node 'id' (adapter 'name')` prefix every AdapterError message carries."""
    return f"node {invocation.node_id!r} (adapter {invocation.adapter!r})"


def _resolve_cli_path(context_label: str) -> str:
    """Locate the ``claude`` CLI on the ambient PATH and return its absolute path.

    Locating the tool is infrastructure: it uses the ambient environment (via
    ``shutil.which``), NOT a node's env allow-list — exactly how ``capability_check``
    already reasons about the ambient env. Returning an absolute path lets the
    caller spawn it with a strict ``env=`` (the node's allow-list for ``invoke``)
    without needing ``PATH`` declared in that allow-list and without leaking it into
    the child. A missing CLI is the Adapter being unable to produce a result at all,
    so it surfaces the same actionable setup AdapterError BEFORE any spawn (#9).
    """
    resolved = shutil.which(CLAUDE_CLI)
    if resolved is None:
        raise AdapterError(f"{context_label}: {_MISSING_CLI_HINT}")
    return resolved


async def _communicate_or_kill(
    process: "asyncio.subprocess.Process",
) -> tuple[bytes, bytes]:
    """``communicate`` on ``process``, killing+reaping its tree on cancellation/timeout.

    The kernel wraps ``invoke`` in ``asyncio.timeout``; when the budget expires the
    awaited ``communicate`` is cancelled (``CancelledError``) — and a bare
    ``TimeoutError`` can surface the same way — leaving the spawned ``claude`` (and
    any grandchild it launched) running. We catch ``BaseException`` (covers both),
    kill the WHOLE process tree by group and reap it so no orphan is left, then
    re-raise so the executor still classifies the node correctly.

    The process is spawned with ``start_new_session=True`` so the whole tree shares a
    process group that ``os.killpg`` signals; a grandchild's inherited stdout pipe
    would otherwise keep this call blocked for its full lifetime. A process that
    already exited (``returncode`` set) needs no kill; ``ProcessLookupError`` is
    suppressed for the race where it exits between the check and the signal.

    NOTE: this mirrors the executor's private ``_kill_and_reap``; it is duplicated
    rather than imported because ``caw.executor`` imports this adapter, so importing
    back would create an import cycle. Consolidating the two copies is tracked in #83.
    """
    try:
        return await process.communicate()
    except BaseException:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        await process.wait()
        raise


class ClaudePrintAdapter(Adapter):
    """Invokes ``claude -p`` (Claude Code headless mode) and normalizes its result."""

    async def invoke(self, invocation: AgentInvocation) -> AgentResult:
        # Locate the CLI on the ambient PATH (infrastructure) and spawn its absolute
        # path, so executable lookup never depends on a PATH inside the node's env
        # allow-list — the child still receives EXACTLY invocation.env (no PATH leak).
        # A missing CLI surfaces the actionable setup error here, before any spawn.
        resolved_cli = _resolve_cli_path(_node_context(invocation))
        # The prompt is positional; the node's `args` pass through verbatim —
        # caw owns no policy engine, so it neither interprets nor injects
        # sandbox/approval (or any) flags. exec (not shell): args are a list, so
        # there is no shell interpolation of the prompt or the passthrough flags.
        argv = [resolved_cli, "-p", invocation.prompt, *invocation.args]
        wants_structured = invocation.output_schema is not None
        if wants_structured:
            # An Output Contract is declared: ask the CLI for its single-object JSON
            # result (`--output-format json`) and hand it the schema so it can shape
            # its structured output (`--json-schema <schema-content>`). The adapter
            # parses the result wrapper; the KERNEL re-validates the schema after
            # invoke returns (ADR 0006) — the adapter never validates it itself.
            schema_text = self._read_schema(invocation)
            argv += ["--output-format", "json", "--json-schema", schema_text]
        # Env policy (ADR 0006, #5): pass EXACTLY the kernel's already-filtered
        # allow-list, never a merge of os.environ — that would leak the parent
        # environment. The consequence is intentional, not a bug: running real
        # `claude` requires the workflow to declare every env var the CLI needs
        # (e.g. its auth/config vars) so they appear in invocation.env.
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=dict(invocation.env),
                # Own session/process group so the whole tree can be killed by group
                # if the kernel's timeout cancels this invoke (see _communicate_or_kill).
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            # Defense-in-depth: _resolve_cli_path already gave a clean pre-spawn
            # missing-CLI error, but a TOCTOU race (the binary vanishing between
            # which and spawn) must still surface the actionable setup error, never
            # a raw FileNotFoundError escaping the Adapter.
            raise AdapterError(f"{_node_context(invocation)}: {_MISSING_CLI_HINT}") from exc
        # communicate, killing+reaping the tree on cancellation/timeout so no orphan
        # claude process is left behind (mirrors the executor's shell-node handling).
        stdout_bytes, stderr_bytes = await _communicate_or_kill(process)
        # backslashreplace (not replace): this stdout feeds State and downstream
        # `when` predicates, so an undecodable byte must decode RECOVERABLY (`\xff`)
        # rather than collapse to an irreversible U+FFFD — matching the executor's
        # shell-node decode.
        stdout = stdout_bytes.decode("utf-8", errors="backslashreplace")
        stderr = stderr_bytes.decode("utf-8", errors="backslashreplace")
        exit_status = process.returncode if process.returncode is not None else -1
        # Parse the result wrapper only on a successful, structured-requested run:
        # a non-zero exit is already a node failure (ADR 0006), so unparseable
        # stdout from a failed process is moot and must NOT mask the real exit.
        structured_output: object | None = None
        if wants_structured and exit_status == 0:
            wrapper = self._parse_result_wrapper(stdout, invocation)
            # Defense-in-depth (#9 review follow-up): `claude` is EXPECTED to exit
            # non-zero when the wrapper reports `is_error: true`, so the process exit
            # code is the PRIMARY failure signal and already catches the common case
            # (a non-zero exit never reaches here — the wrapper is not even parsed).
            # This guards the uncertain edge where the process exits 0 yet the wrapper
            # says `is_error: true` — known for some subtypes such as `error_max_turns`
            # (ref anthropics/claude-code-action#823). It is NOT the primary path.
            if wrapper.get("is_error") is True:
                # Normalize to a FAILED node: force a non-zero exit_status, drop the
                # structured_output (a failed node carries no trustworthy output,
                # matching the existing non-zero-exit behavior — and the kernel skips
                # Output Contract validation for a non-zero exit per #63), and append
                # an actionable annotation to stderr (preserving any the process
                # emitted). stdout keeps the raw wrapper so the trace stays complete.
                exit_status = 1
                stderr = self._annotate_cli_error(stderr, wrapper)
            elif "structured_output" not in wrapper:
                # The adapter asked for structured output (via --json-schema) but the
                # wrapper carries NO `structured_output` key — the CLI produced none,
                # so the adapter cannot produce a result (an AdapterError, consistent
                # with the unparseable-when-required case above). Crucially we must
                # distinguish an ABSENT key from an explicit JSON `null`: collapsing
                # both to None via .get() would defeat the kernel's deliberate
                # null-vs-absent distinction (the kernel's schema is the sole arbiter
                # of whether a present null satisfies the Output Contract, ADR 0006).
                raise AdapterError(
                    f"{_node_context(invocation)}: 'claude -p --output-format json' "
                    "produced no 'structured_output' field but one was required by "
                    "the node's output_schema"
                )
            else:
                # The key is present: pass its value through AS-IS, including an
                # explicit JSON null (-> Python None). The kernel re-validates it
                # against the Output Contract (ADR 0006).
                structured_output = wrapper["structured_output"]
        return AgentResult(
            exit_status=exit_status,
            stdout=stdout,
            stderr=stderr,
            structured_output=structured_output,
        )

    async def capability_check(self) -> str:
        """Probe the installed ``claude`` CLI and return its version string.

        This is adapter INFRASTRUCTURE, not a node invocation: it locates the CLI on
        the ambient PATH (via :func:`_resolve_cli_path`, the same locate-and-error
        path :meth:`invoke` uses) and probes ``claude --version`` in the ambient
        environment — no node-declared env allow-list applies, unlike
        :meth:`invoke`, so the spawn passes no ``env=``. The version is returned to
        the caller and kept adapter-local — it is NOT persisted to State (token/cost/
        version surfacing is carved out to #79). A missing CLI surfaces the same
        actionable setup AdapterError as invoke, before any spawn.
        """
        resolved_cli = _resolve_cli_path("capability check")
        try:
            process = await asyncio.create_subprocess_exec(
                resolved_cli,
                "--version",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                # Own session/process group so a cancelled probe can be killed by
                # group, leaving no orphan (see _communicate_or_kill).
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            # Defense-in-depth for the TOCTOU race (see invoke): the binary
            # disappearing between which and spawn still yields the setup error.
            raise AdapterError(f"capability check: {_MISSING_CLI_HINT}") from exc
        stdout_bytes, stderr_bytes = await _communicate_or_kill(process)
        if process.returncode != 0:
            stderr = stderr_bytes.decode("utf-8", errors="backslashreplace").strip()
            raise AdapterError(
                f"capability check: 'claude --version' exited "
                f"{process.returncode}: {stderr or '<no stderr>'}"
            )
        return stdout_bytes.decode("utf-8", errors="backslashreplace").strip()

    @staticmethod
    def _read_schema(invocation: AgentInvocation) -> str:
        schema = invocation.output_schema
        assert schema is not None  # guarded by the caller
        try:
            return schema.read_text(encoding="utf-8")
        except OSError as exc:
            raise AdapterError(
                f"{_node_context(invocation)}: cannot read output_schema {schema}: {exc}"
            ) from exc

    @staticmethod
    def _parse_result_wrapper(stdout: str, invocation: AgentInvocation) -> dict[str, object]:
        """Parse the CLI's single JSON result wrapper and return it as a dict.

        The CLI prints a single JSON object carrying the freeform `result` text, a
        top-level `structured_output` field (the schema-shaped value, when a
        `--json-schema` was supplied), and the run status (`is_error`, `subtype`).
        `invoke` reads those fields off the returned dict to extract the structured
        output OR — on the `is_error: true` edge — normalize the result to a failed
        node (#9 review follow-up). Unparseable stdout (or a non-object wrapper) when
        structured output was REQUIRED means the adapter cannot produce a result — an
        AdapterError, per ADR 0006. The wrapper is returned as-is; the kernel remains
        the sole arbiter of whether `structured_output` satisfies the Output Contract.

        Detecting `is_error` REQUIRES this JSON wrapper, so that check is scoped to the
        structured path; the freeform path (no `output_schema`) has no wrapper to
        inspect and relies on the exit code + stderr alone. Unifying wrapper parsing
        for the freeform path is deferred — #79 parses this same wrapper for usage/cost.
        """
        try:
            wrapper = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise AdapterError(
                f"{_node_context(invocation)}: expected a JSON result from "
                f"'claude -p --output-format json' but could not parse stdout: {exc}"
            ) from exc
        if not isinstance(wrapper, dict):
            raise AdapterError(
                f"{_node_context(invocation)}: expected a JSON object from "
                f"'claude -p --output-format json', got {type(wrapper).__name__}"
            )
        return wrapper

    @staticmethod
    def _annotate_cli_error(stderr: str, wrapper: dict[str, object]) -> str:
        """Append an actionable `claude reported an error` annotation to `stderr`.

        Names the wrapper's `subtype` when present (e.g. `error_max_turns`) so the
        failure is diagnosable from the trace, and PRESERVES any stderr the process
        already emitted by appending rather than clobbering (#9 review follow-up).
        The process stderr is rstripped before the join so a process whose stderr
        already ends in a newline does not get a doubled/trailing blank line: the
        is_error path forces exit_status=1, so the executor's exit==0-only `.strip()`
        never cleans this persisted stderr.
        """
        subtype = wrapper.get("subtype")
        annotation = "claude reported an error"
        if isinstance(subtype, str) and subtype:
            annotation += f" (subtype: {subtype})"
        return f"{stderr.rstrip()}\n{annotation}" if stderr.strip() else annotation
