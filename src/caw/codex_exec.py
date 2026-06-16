"""The ``codex.exec`` Adapter: invokes ``codex exec`` headless mode (#11).

This is the second real Adapter behind the vendor-neutral interface of ADR 0006,
capability-symmetric with :mod:`caw.claude_print` (#11): a node switches between
the two by changing ONLY its adapter name. It spawns Codex's non-interactive
``codex exec`` and normalizes the process into an :class:`AgentResult`. ALL
``codex``-specific knowledge — the ``exec`` subcommand, the ``--json`` JSONL event
stream, ``--output-schema``, the ``agent_message`` item that carries the final
message, and the ``turn.failed`` event — lives here and nowhere in the kernel, so
the executor, State, and Events stay vendor-neutral.

Where ``claude -p --output-format json`` prints a single JSON wrapper, ``codex exec
--json`` prints a stream of JSONL events; the adapter folds that stream to the same
vendor-neutral shape claude.print produces (a final message text + an optional
structured object), so the kernel cannot tell the two apart.
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
# so a shell-only or offline Run never requires `codex`. Locating the binary uses
# the ambient PATH (infrastructure); it is deliberately NOT part of a node's env
# allow-list, so the child still receives only invocation.env (see _resolve_cli_path).
CODEX_CLI = "codex"

# The actionable setup message a missing CLI surfaces. ADR 0006 reserves AdapterError
# for the Adapter being unable to produce a result at all; a CLI that is not installed
# is exactly that, so the message tells the user how to install or enable it rather
# than leaking a raw FileNotFoundError.
_MISSING_CLI_HINT = (
    "the 'codex' CLI was not found on PATH. Install Codex CLI "
    "(https://developers.openai.com/codex/cli) and ensure 'codex' is on PATH, "
    "or run this node through the 'mock' adapter offline."
)


def _node_context(invocation: AgentInvocation) -> str:
    """The `node 'id' (adapter 'name')` prefix every AdapterError message carries."""
    return f"node {invocation.node_id!r} (adapter {invocation.adapter!r})"


def _resolve_cli_path(context_label: str) -> str:
    """Locate the ``codex`` CLI on the ambient PATH and return its absolute path.

    Locating the tool is infrastructure: it uses the ambient environment (via
    ``shutil.which``), NOT a node's env allow-list — exactly how ``capability_check``
    already reasons about the ambient env. Returning an absolute path lets the caller
    spawn it with a strict ``env=`` (the node's allow-list for ``invoke``) without
    needing ``PATH`` declared in that allow-list and without leaking it into the
    child. A missing CLI is the Adapter being unable to produce a result at all, so it
    surfaces the same actionable setup AdapterError BEFORE any spawn (#11).
    """
    resolved = shutil.which(CODEX_CLI)
    if resolved is None:
        raise AdapterError(f"{context_label}: {_MISSING_CLI_HINT}")
    return resolved


async def _communicate_or_kill(
    process: "asyncio.subprocess.Process",
) -> tuple[bytes, bytes]:
    """``communicate`` on ``process``, killing+reaping its tree on cancellation/timeout.

    The kernel wraps ``invoke`` in ``asyncio.timeout``; when the budget expires the
    awaited ``communicate`` is cancelled (``CancelledError``) — and a bare
    ``TimeoutError`` can surface the same way — leaving the spawned ``codex`` (and any
    grandchild it launched) running. We catch ``BaseException`` (covers both), kill the
    WHOLE process tree by group and reap it so no orphan is left, then re-raise so the
    executor still classifies the node correctly.

    The process is spawned with ``start_new_session=True`` so the whole tree shares a
    process group that ``os.killpg`` signals; a grandchild's inherited stdout pipe would
    otherwise keep this call blocked for its full lifetime. The leader's ``returncode``
    being set does NOT mean the process GROUP is dead, so the group is signalled
    REGARDLESS of the leader's returncode. ``ProcessLookupError`` is suppressed for the
    case where no group member remains.

    NOTE: this mirrors the executor's private ``_kill_and_reap`` and the identical
    helper in ``caw.claude_print``; it is duplicated rather than imported because
    ``caw.executor`` imports this adapter, so importing back would create an import
    cycle. Consolidating the copies is tracked in #83.
    """
    try:
        return await process.communicate()
    except BaseException:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()
        raise


class CodexExecAdapter(Adapter):
    """Invokes ``codex exec`` (Codex headless mode) and normalizes its result.

    Capability-symmetric with :class:`caw.claude_print.ClaudePrintAdapter`: same
    locate-then-spawn discipline, same env policy, same cancellation/timeout teardown,
    and the same vendor-neutral :class:`AgentResult` shape — so a node switches between
    ``claude.print`` and ``codex.exec`` by changing only its adapter name (#11).
    """

    async def invoke(self, invocation: AgentInvocation) -> AgentResult:
        # Locate the CLI on the ambient PATH (infrastructure) and spawn its absolute
        # path, so executable lookup never depends on a PATH inside the node's env
        # allow-list — the child still receives EXACTLY invocation.env (no PATH leak).
        # A missing CLI surfaces the actionable setup error here, before any spawn.
        resolved_cli = _resolve_cli_path(_node_context(invocation))
        # `--json` is the analogue of claude's `--output-format json`: it makes codex
        # emit a parseable JSONL event stream the adapter folds to a final message
        # (+ optional structured object). The node's `args` pass through verbatim —
        # caw owns no policy engine, so sandbox/approval flags are ordinary passthrough
        # args (#11 acceptance 3), neither interpreted nor injected here.
        argv = [resolved_cli, "exec", "--json", *invocation.args]
        wants_structured = invocation.output_schema is not None
        if wants_structured:
            # An Output Contract is declared: hand codex the schema FILE PATH so it can
            # shape its structured output (`--output-schema <path>`). Unlike claude
            # (which takes inline schema TEXT), codex reads the schema from a file. The
            # adapter parses the agent_message; the KERNEL re-validates the schema after
            # invoke returns (ADR 0006) — the adapter never validates it itself.
            schema_path = self._schema_path(invocation)
            argv += ["--output-schema", schema_path]
        # The `--` separator and the prompt are ALWAYS appended last, so the prompt is
        # the final token after every flag — a leading-dash prompt (e.g. "--help") can
        # never be parsed by `codex exec` as a flag.
        argv += ["--", invocation.prompt]
        # Env policy (ADR 0006, #5): pass EXACTLY the kernel's already-filtered
        # allow-list, never a merge of os.environ — that would leak the parent
        # environment. Running real `codex` requires the workflow to declare every env
        # var the CLI needs (e.g. its auth/config vars) so they appear in invocation.env.
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                # codex exec reads supplementary input from stdin; DEVNULL stops a real
                # invocation from blocking forever waiting for piped input.
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=dict(invocation.env),
                # Own session/process group so the whole tree can be killed by group if
                # the kernel's timeout cancels this invoke (see _communicate_or_kill).
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            # Defense-in-depth: _resolve_cli_path already gave a clean pre-spawn
            # missing-CLI error, but a TOCTOU race (the binary vanishing between which
            # and spawn) must still surface the actionable setup error, never a raw
            # FileNotFoundError escaping the Adapter.
            raise AdapterError(f"{_node_context(invocation)}: {_MISSING_CLI_HINT}") from exc
        stdout_bytes, stderr_bytes = await _communicate_or_kill(process)
        # backslashreplace (not replace): this output feeds State and downstream `when`
        # predicates, so an undecodable byte must decode RECOVERABLY (`\xff`) rather than
        # collapse to an irreversible U+FFFD — matching the executor's shell-node decode.
        raw_stdout = stdout_bytes.decode("utf-8", errors="backslashreplace")
        stderr = stderr_bytes.decode("utf-8", errors="backslashreplace")
        exit_status = process.returncode if process.returncode is not None else -1
        # A non-zero exit is already a node failure (ADR 0006): the raw stdout is the
        # process's own output and the JSONL events (if any) are moot, so the failure
        # is surfaced AS-IS without parsing — the exit code is the primary signal and
        # must not be masked by an unparseable stream.
        if exit_status != 0:
            return AgentResult(exit_status=exit_status, stdout=raw_stdout, stderr=stderr)
        # A zero exit: fold the JSONL event stream to the final agent message and,
        # when a schema was required, the structured object parsed from it.
        events = self._parse_events(raw_stdout)
        failure = self._turn_failure(events)
        if failure:
            # Defense-in-depth, symmetric with claude.print's is_error handling: codex
            # can report a `turn.failed` event even on a ZERO process exit. Normalize to
            # a FAILED node — force a non-zero exit_status, drop any structured_output (a
            # failed node carries no trustworthy output, and the kernel skips Output
            # Contract validation for a non-zero exit per #63), and append an actionable
            # annotation carrying codex's error message. raw_stdout keeps the full event
            # stream so the trace stays complete.
            return AgentResult(
                exit_status=1,
                stdout=raw_stdout,
                stderr=self._annotate_cli_error(stderr, failure),
            )
        message = self._final_agent_message(events)
        structured_output: object | None = None
        stdout = raw_stdout
        if message is not None:
            # The agent's final message is the vendor-neutral stdout (symmetric with
            # claude.print's freeform text), even on the structured path where it is a
            # JSON string.
            stdout = message
        if wants_structured:
            if message is None:
                # Asked for structured output (via --output-schema) but the stream
                # carried no agent_message — codex produced none, so the adapter cannot
                # produce a result (an AdapterError, consistent with the unparseable
                # case below).
                raise AdapterError(
                    f"{_node_context(invocation)}: 'codex exec --json' produced no "
                    "agent message but a structured result was required by the node's "
                    "output_schema"
                )
            structured_output = self._parse_structured_message(message, invocation)
        return AgentResult(
            exit_status=exit_status,
            stdout=stdout,
            stderr=stderr,
            structured_output=structured_output,
        )

    async def capability_check(self) -> str:
        """Probe the installed ``codex`` CLI and return its version string.

        This is adapter INFRASTRUCTURE, not a node invocation: it locates the CLI on the
        ambient PATH (via :func:`_resolve_cli_path`, the same locate-and-error path
        :meth:`invoke` uses) and probes ``codex --version`` in the ambient environment —
        no node-declared env allow-list applies, unlike :meth:`invoke`, so the spawn
        passes no ``env=``. The version is returned to the caller and kept adapter-local
        — it is NOT persisted to State (token/cost/version surfacing is carved out to
        #79). A missing CLI surfaces the same actionable setup AdapterError as invoke,
        before any spawn.
        """
        resolved_cli = _resolve_cli_path("capability check")
        try:
            process = await asyncio.create_subprocess_exec(
                resolved_cli,
                "--version",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                # Own session/process group so a cancelled probe can be killed by group,
                # leaving no orphan (see _communicate_or_kill).
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            # Defense-in-depth for the TOCTOU race (see invoke): the binary disappearing
            # between which and spawn still yields the setup error.
            raise AdapterError(f"capability check: {_MISSING_CLI_HINT}") from exc
        stdout_bytes, stderr_bytes = await _communicate_or_kill(process)
        if process.returncode != 0:
            stderr = stderr_bytes.decode("utf-8", errors="backslashreplace").strip()
            raise AdapterError(
                f"capability check: 'codex --version' exited "
                f"{process.returncode}: {stderr or '<no stderr>'}"
            )
        return stdout_bytes.decode("utf-8", errors="backslashreplace").strip()

    @staticmethod
    def _schema_path(invocation: AgentInvocation) -> str:
        """The output_schema as a filesystem path string for ``--output-schema``.

        codex reads the JSON Schema from a FILE (unlike claude's inline schema text), so
        the adapter passes the resolved path through. Existence is the CLI's concern at
        spawn time — a missing file surfaces as a normal non-zero ``codex`` exit, an
        ordinary AgentResult, not an AdapterError.
        """
        schema = invocation.output_schema
        assert schema is not None  # guarded by the caller
        return str(schema)

    @staticmethod
    def _parse_events(stdout: str) -> list[dict[str, object]]:
        """Parse ``codex exec --json`` JSONL stdout into a list of event objects.

        Each non-blank line is one JSON object; a line that is not a JSON object is
        skipped (codex may interleave non-event lines). An entirely unparseable stream
        when structured output was REQUIRED would leave no agent_message and surface as
        the absent-message AdapterError in :meth:`invoke`; here we tolerate stray lines
        so a single malformed line never discards the whole run.
        """
        events: list[dict[str, object]] = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                events.append(event)
        return events

    @staticmethod
    def _final_agent_message(events: list[dict[str, object]]) -> str | None:
        """The text of the LAST ``agent_message`` item in the event stream, or None.

        codex emits the agent's final message as an ``item.completed`` event whose
        ``item.type`` is ``agent_message`` and whose ``item.text`` is the message (the
        structured-output JSON string when an ``--output-schema`` was supplied). The
        last such item is the final answer.
        """
        text: str | None = None
        for event in events:
            if event.get("type") != "item.completed":
                continue
            item = event.get("item")
            if not isinstance(item, dict) or item.get("type") != "agent_message":
                continue
            value = item.get("text")
            if isinstance(value, str):
                text = value
        return text

    @staticmethod
    def _turn_failure(events: list[dict[str, object]]) -> str:
        """The error message of a ``turn.failed`` / ``error`` event, or ``""`` if none.

        codex can report a failure in its event stream even on a zero process exit (a
        ``turn.failed`` event, or a top-level ``error`` event). :meth:`invoke` uses a
        non-empty return to normalize the run to a FAILED node — symmetric with
        claude.print's ``is_error`` handling. Returns the codex error message for the
        actionable annotation, or ``""`` when the run carries no failure event.
        """
        for event in events:
            event_type = event.get("type")
            if event_type == "turn.failed":
                error = event.get("error")
                if isinstance(error, dict):
                    message = error.get("message")
                    if isinstance(message, str) and message:
                        return message
                return "codex reported a failed turn"
            if event_type == "error":
                message = event.get("message")
                if isinstance(message, str) and message:
                    return message
                return "codex reported an error"
        return ""

    @staticmethod
    def _parse_structured_message(message: str, invocation: AgentInvocation) -> object:
        """Parse the agent_message text as the structured object, or fail cleanly.

        On the structured path codex's final agent_message IS the JSON value shaped by
        the ``--output-schema``. The adapter parses it and passes it through AS-IS; the
        kernel re-validates it against the Output Contract (ADR 0006), so the schema is
        the sole arbiter of whether the value satisfies the contract. Unparseable text
        when structured output was REQUIRED means the adapter cannot produce a result —
        an AdapterError, per ADR 0006.
        """
        try:
            return json.loads(message)
        except json.JSONDecodeError as exc:
            raise AdapterError(
                f"{_node_context(invocation)}: expected a JSON structured result from "
                f"'codex exec --json --output-schema' but could not parse the agent "
                f"message: {exc}"
            ) from exc

    @staticmethod
    def _annotate_cli_error(stderr: str, message: str) -> str:
        """Append an actionable `codex reported an error` annotation to ``stderr``.

        Carries codex's error ``message`` so the failure is diagnosable from the trace,
        and PRESERVES any stderr the process already emitted by appending rather than
        clobbering (symmetric with claude.print). The process stderr is rstripped before
        the join so a process whose stderr already ends in a newline does not get a
        doubled/trailing blank line: the turn-failed path forces exit_status=1, so the
        executor's exit==0-only `.strip()` never cleans this persisted stderr.
        """
        annotation = f"codex reported an error: {message}"
        return f"{stderr.rstrip()}\n{annotation}" if stderr.strip() else annotation
