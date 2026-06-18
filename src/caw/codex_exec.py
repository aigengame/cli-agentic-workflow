"""The ``codex.exec`` Adapter: invokes ``codex exec`` headless mode (#11).

This is the second real Adapter behind the vendor-neutral interface of ADR 0006,
capability-symmetric with :mod:`caw.claude_print` (#11): a node switches between the
two by changing ONLY its adapter name. It spawns Codex's non-interactive ``codex exec``
and normalizes the process into an :class:`AgentResult`. ALL ``codex``-specific
knowledge — the ``exec`` subcommand, the ``--json`` JSONL event stream,
``--output-schema``, the ``agent_message`` item that carries the final message, and the
``turn.failed`` event — lives here and nowhere in the kernel, so the executor, State,
and Events stay vendor-neutral.

The cross-cutting subprocess machinery — locating the CLI, spawning it with the strict
node env, stdin isolation, the process-group lifecycle, the version probe, and the
missing-CLI error — is the shared :class:`caw.subprocess_adapter.SubprocessAdapter` base
(#83), the SAME base ``claude.print`` uses; this module keeps only the codex-specific
argv construction and JSONL-stream parsing.

Where ``claude -p --output-format json`` prints a single JSON wrapper, ``codex exec
--json`` prints a STREAM of JSONL events; the adapter folds that stream to the same
vendor-neutral shape claude.print produces (a final message text + an optional
structured object), so the kernel cannot tell the two apart.
"""

import json

from caw.adapter import Adapter, AdapterError, AgentInvocation, AgentResult
from caw.subprocess_adapter import SubprocessAdapter, node_context

# The CLI entrypoint this Adapter drives. Resolved to an absolute path with shutil.which
# at invoke / capability-check time (lazily, by the base), never at construction, so a
# shell-only or offline Run never requires `codex`. Locating the binary uses the ambient
# PATH (infrastructure); it is deliberately NOT part of a node's env allow-list, so the
# child still receives only invocation.env.
CODEX_CLI = "codex"

# The actionable setup message a missing CLI surfaces. ADR 0006 reserves AdapterError for
# the Adapter being unable to produce a result at all; a CLI that is not installed is
# exactly that, so the message tells the user how to install or enable it rather than
# leaking a raw FileNotFoundError.
_MISSING_CLI_HINT = (
    "the 'codex' CLI was not found on PATH. Install Codex CLI "
    "(https://developers.openai.com/codex/cli) and ensure 'codex' is on PATH, "
    "or run this node through the 'mock' adapter offline."
)


class CodexExecAdapter(SubprocessAdapter, Adapter):
    """Invokes ``codex exec`` (Codex headless mode) and normalizes its result.

    Inherits the subprocess lifecycle (locate + spawn + kill/reap + returncode
    pass-through) and the version probe from :class:`SubprocessAdapter` (#83); supplies
    the ``codex`` binary name, the missing-CLI hint, and the codex-specific argv and
    JSONL-stream handling. Capability-symmetric with
    :class:`caw.claude_print.ClaudePrintAdapter`: a node switches between ``claude.print``
    and ``codex.exec`` by changing only its adapter name (#11).
    """

    cli_name = CODEX_CLI
    missing_cli_hint = _MISSING_CLI_HINT

    async def invoke(self, invocation: AgentInvocation) -> AgentResult:
        # Locate the CLI on the ambient PATH (infrastructure) and spawn its absolute
        # path, so executable lookup never depends on a PATH inside the node's env
        # allow-list — the child still receives EXACTLY invocation.env (no PATH leak). A
        # missing CLI surfaces the actionable setup error here (the base), before spawn.
        resolved_cli = self.resolve_cli_path(node_context(invocation))
        # `--json` is the analogue of claude's `--output-format json`: it makes codex emit
        # a parseable JSONL event stream the adapter folds to a final message (+ optional
        # structured object). The node's `args` pass through verbatim — caw owns no policy
        # engine, so sandbox/approval flags are ordinary passthrough args (#11 acceptance
        # 3), neither interpreted nor injected here.
        argv = [resolved_cli, "exec", "--json", *invocation.args]
        wants_structured = invocation.output_schema is not None
        if wants_structured:
            # An Output Contract is declared: hand codex the schema FILE PATH so it can
            # shape its structured output (`--output-schema <path>`). Unlike claude (which
            # takes inline schema TEXT), codex reads the schema from a file. The adapter
            # parses the agent_message; the KERNEL re-validates the schema after invoke
            # returns (ADR 0006) — the adapter never validates it itself.
            argv += ["--output-schema", self._schema_path(invocation)]
        # The `--` separator and the prompt are ALWAYS appended last, so the prompt is the
        # final token after every flag — a leading-dash prompt (e.g. "--help") can never
        # be parsed by `codex exec` as a flag.
        argv += ["--", invocation.prompt]
        # Env policy (ADR 0006, #5): the base passes EXACTLY the kernel's already-filtered
        # allow-list to the child, never a merge of os.environ — that would leak the
        # parent environment. Running real `codex` requires the workflow to declare every
        # env var the CLI needs (e.g. its auth/config vars) so they appear in
        # invocation.env. The base isolates stdin with DEVNULL (codex exec reads
        # supplementary input from stdin, which would otherwise block) and kills+reaps the
        # whole tree on a cancellation/timeout.
        completed = await self.run_cli(
            argv,
            context_label=node_context(invocation),
            env=invocation.env,
            cwd=invocation.working_dir,
        )
        exit_status = completed.returncode
        stdout = completed.stdout
        stderr = completed.stderr
        # A non-zero exit is already a node failure (ADR 0006): the raw stdout is the
        # process's own output and the JSONL events (if any) are moot, so the failure is
        # surfaced AS-IS without parsing — the exit code is the primary signal and must
        # not be masked by an unparseable stream.
        if exit_status != 0:
            return AgentResult(
                exit_status=exit_status,
                stdout=stdout,
                stderr=stderr,
                artifacts=completed.artifacts,
            )
        # A zero exit: fold the JSONL event stream to the final agent message and, when a
        # schema was required, the structured object parsed from it.
        events = self._parse_events(stdout)
        failure = self._turn_failure(events)
        if failure:
            # Defense-in-depth, symmetric with claude.print's is_error handling: codex can
            # report a `turn.failed` event even on a ZERO process exit. Normalize to a
            # FAILED node via the first-class adapter-determined-failure signal (ADR 0006,
            # #84): raise `adapter_failure` and KEEP the process's real exit_status (here
            # 0) rather than fabricating a non-zero exit through the exit-code channel. The
            # kernel honors the flag once, drops any structured_output (a failed node
            # carries none, and the kernel skips Output Contract validation for an
            # adapter-determined failure, #63), and an actionable annotation carries
            # codex's error message. stdout keeps the full event stream so the trace stays
            # complete.
            return AgentResult(
                exit_status=exit_status,
                stdout=stdout,
                stderr=self._annotate_cli_error(stderr, failure),
                artifacts=completed.artifacts,
                adapter_failure=True,
            )
        message = self._final_agent_message(events)
        structured_output: object | None = None
        if message is not None:
            # The agent's final message is the vendor-neutral stdout (symmetric with
            # claude.print's freeform text), even on the structured path where it is a
            # JSON string.
            stdout = message
        if wants_structured:
            if message is None:
                # Asked for structured output (via --output-schema) but the stream carried
                # no agent_message — codex produced none, so the adapter cannot produce a
                # result (an AdapterError, consistent with the unparseable case below).
                raise AdapterError(
                    f"{node_context(invocation)}: 'codex exec --json' produced no "
                    "agent message but a structured result was required by the node's "
                    "output_schema"
                )
            structured_output = self._parse_structured_message(message, invocation)
        return AgentResult(
            exit_status=exit_status,
            stdout=stdout,
            stderr=stderr,
            structured_output=structured_output,
            artifacts=completed.artifacts,
        )

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
        for raw_line in stdout.splitlines():
            line = raw_line.strip()
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
        structured-output JSON string when an ``--output-schema`` was supplied). The last
        such item is the final answer.
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

        On the structured path codex's final agent_message IS the JSON value shaped by the
        ``--output-schema``. The adapter parses it and passes it through AS-IS; the kernel
        re-validates it against the Output Contract (ADR 0006), so the schema is the sole
        arbiter of whether the value satisfies the contract. Any JSON value is accepted
        here (symmetric with claude.print, whose ``structured_output`` need not be an
        object); unparseable text when structured output was REQUIRED means the adapter
        cannot produce a result — an AdapterError, per ADR 0006.
        """
        try:
            return json.loads(message)
        except json.JSONDecodeError as exc:
            raise AdapterError(
                f"{node_context(invocation)}: expected a JSON structured result from "
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
        doubled/trailing blank line: the turn-failed path raises ``adapter_failure``, so
        the node is failed and the executor's success-only ``.strip()`` never cleans this
        persisted stderr.
        """
        annotation = f"codex reported an error: {message}"
        return f"{stderr.rstrip()}\n{annotation}" if stderr.strip() else annotation
