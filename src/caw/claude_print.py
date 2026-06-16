"""The ``claude.print`` Adapter: invokes ``claude -p`` headless mode (#9).

This is the first real Adapter behind the vendor-neutral interface of ADR 0006:
it spawns Claude Code's print/headless mode (``claude -p``) and normalizes the
process into an :class:`AgentResult`. ALL ``claude``-specific knowledge — the
``-p`` flag, ``--output-format json``, ``--json-schema``, and the result-wrapper
shape — lives here and nowhere in the kernel, so the executor, State, and Events
stay vendor-neutral.

The cross-cutting subprocess machinery — locating the CLI, spawning it with the
strict node env, the process-group lifecycle, and the missing-CLI error — is the
shared :class:`caw.subprocess_adapter.SubprocessAdapter` base (#83), which
``codex.exec`` (#11) reuses unchanged; this module keeps only the claude-specific
argv construction and result-wrapper parsing.
"""

from caw.adapter import Adapter, AdapterError, AgentInvocation, AgentResult
from caw.subprocess_adapter import SubprocessAdapter, node_context, parse_json_object

# The CLI entrypoint this Adapter drives. Resolved to an absolute path with
# shutil.which at invoke / capability-check time (lazily, by the base), never at
# construction, so a shell-only or offline Run never requires `claude`. Locating the
# binary uses the ambient PATH (infrastructure); it is deliberately NOT part of a
# node's env allow-list, so the child still receives only invocation.env.
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


class ClaudePrintAdapter(SubprocessAdapter, Adapter):
    """Invokes ``claude -p`` (Claude Code headless mode) and normalizes its result.

    Inherits the subprocess lifecycle (locate + spawn + kill/reap + returncode
    pass-through) and the version probe from :class:`SubprocessAdapter`; supplies the
    ``claude`` binary name, the missing-CLI hint, and the claude-specific argv and
    result-wrapper handling.
    """

    cli_name = CLAUDE_CLI
    missing_cli_hint = _MISSING_CLI_HINT

    async def invoke(self, invocation: AgentInvocation) -> AgentResult:
        # Locate the CLI on the ambient PATH (infrastructure) and spawn its absolute
        # path, so executable lookup never depends on a PATH inside the node's env
        # allow-list — the child still receives EXACTLY invocation.env (no PATH leak).
        # A missing CLI surfaces the actionable setup error here, before any spawn.
        resolved_cli = self.resolve_cli_path(node_context(invocation))
        # The prompt is the TRAILING positional, placed after a `--` end-of-options
        # separator so a leading-dash prompt (e.g. "--help") can never be parsed by
        # `claude` as a flag: every flag/arg goes BEFORE `--`, the prompt AFTER it.
        # The node's `args` pass through verbatim — caw owns no policy engine, so it
        # neither interprets nor injects sandbox/approval (or any) flags. exec (not
        # shell): args are a list, so there is no shell interpolation of the prompt or
        # the passthrough flags.
        argv = [resolved_cli, "-p", *invocation.args]
        wants_structured = invocation.output_schema is not None
        if wants_structured:
            # An Output Contract is declared: ask the CLI for its single-object JSON
            # result (`--output-format json`) and hand it the schema so it can shape
            # its structured output (`--json-schema <schema-content>`). The adapter
            # parses the result wrapper; the KERNEL re-validates the schema after
            # invoke returns (ADR 0006) — the adapter never validates it itself.
            #
            # The schema CONTENT is inlined as an argv element because the real
            # `claude --json-schema <schema>` flag takes the schema text, NOT a file
            # path (verified against the CLI for #84); there is no `--json-schema-file`
            # equivalent, so passing a path is not an option the CLI supports.
            schema_text = self._read_schema(invocation)
            argv += ["--output-format", "json", "--json-schema", schema_text]
        # The `--` separator and the prompt are ALWAYS appended last, so the prompt is
        # the final token after every flag (passthrough and structured alike).
        argv += ["--", invocation.prompt]
        # Env policy (ADR 0006, #5): the base passes EXACTLY the kernel's
        # already-filtered allow-list to the child, never a merge of os.environ — that
        # would leak the parent environment. The consequence is intentional, not a bug:
        # running real `claude` requires the workflow to declare every env var the CLI
        # needs (e.g. its auth/config vars) so they appear in invocation.env. A
        # cancellation/timeout kills and reaps the whole tree (the base's lifecycle).
        completed = await self.run_cli(
            argv, context_label=node_context(invocation), env=invocation.env
        )
        exit_status = completed.returncode
        stdout = completed.stdout
        stderr = completed.stderr
        # Parse the result wrapper only on a successful, structured-requested run:
        # a non-zero exit is already a node failure (ADR 0006), so unparseable
        # stdout from a failed process is moot and must NOT mask the real exit.
        structured_output: object | None = None
        adapter_failure = False
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
                # Normalize to a FAILED node via the first-class adapter-determined-
                # failure signal (ADR 0006, #83): raise `adapter_failure` and KEEP the
                # process's real exit_status (here 0) rather than fabricating a non-zero
                # exit through the exit-code channel. The kernel honors the flag once.
                # Drop the structured_output (a failed node carries no trustworthy
                # output — the kernel also skips Output Contract validation for an
                # adapter-determined failure, per #63), and append an actionable
                # annotation to stderr (preserving any the process emitted). stdout
                # keeps the raw wrapper so the trace stays complete.
                adapter_failure = True
                stderr = self._annotate_cli_error(stderr, wrapper)
            elif "structured_output" not in wrapper:
                # The adapter asked for structured output (via --json-schema) but the
                # wrapper carries NO `structured_output` key — the CLI produced none,
                # so the adapter cannot produce a result (an AdapterError, consistent
                # with the unparseable-when-required case above). An ABSENT key is
                # distinguished from a present explicit JSON `null` here, but ONLY for
                # the kernel's Output-Contract validation: an absent key means claude
                # produced nothing, while a present null is a value claude produced.
                # Note this distinction does NOT survive downstream — see the present-
                # key branch below.
                raise AdapterError(
                    f"{node_context(invocation)}: 'claude -p --output-format json' "
                    "produced no 'structured_output' field but one was required by "
                    "the node's output_schema"
                )
            else:
                # The key is present (including an explicit JSON null -> Python None):
                # pass its value through AS-IS. The kernel re-validates it against the
                # Output Contract, where the schema is the sole arbiter of whether a
                # present null satisfies the contract (ADR 0006). The null-vs-absent
                # distinction holds for Output-Contract validation ONLY: downstream,
                # caw deliberately does NOT distinguish a produced `null` from
                # produced-nothing (#75 decision, ADR 0007) — executor.normalized_output
                # omits structured_output when it is None, predicate._evaluate_leaf
                # treats an absent field as False, and `equals null` is rejected at
                # validation, so in State and in `when` predicates a null reads the
                # same as an absent field.
                structured_output = wrapper["structured_output"]
        return AgentResult(
            exit_status=exit_status,
            stdout=stdout,
            stderr=stderr,
            structured_output=structured_output,
            adapter_failure=adapter_failure,
        )

    @staticmethod
    def _read_schema(invocation: AgentInvocation) -> str:
        schema = invocation.output_schema
        assert schema is not None  # guarded by the caller
        try:
            return schema.read_text(encoding="utf-8")
        except OSError as exc:
            raise AdapterError(
                f"{node_context(invocation)}: cannot read output_schema {schema}: {exc}"
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
        AdapterError, per ADR 0006, surfaced through the shared JSON ladder
        (:func:`parse_json_object`, #83). The wrapper is returned as-is; the kernel
        remains the sole arbiter of whether `structured_output` satisfies the Output
        Contract.

        Detecting `is_error` REQUIRES this JSON wrapper, so that check is scoped to the
        structured path; the freeform path (no `output_schema`) has no wrapper to
        inspect and relies on the exit code + stderr alone. Unifying wrapper parsing
        for the freeform path is deferred — #79 parses this same wrapper for usage/cost.
        """
        return parse_json_object(
            stdout,
            context=node_context(invocation),
            source_label="'claude -p --output-format json'",
        )

    @staticmethod
    def _annotate_cli_error(stderr: str, wrapper: dict[str, object]) -> str:
        """Append an actionable `claude reported an error` annotation to `stderr`.

        Names the wrapper's `subtype` when present (e.g. `error_max_turns`) so the
        failure is diagnosable from the trace, and PRESERVES any stderr the process
        already emitted by appending rather than clobbering (#9 review follow-up).
        The process stderr is rstripped before the join so a process whose stderr
        already ends in a newline does not get a doubled/trailing blank line: the
        is_error path raises `adapter_failure` (#83), so the node is failed and the
        executor's success-only `.strip()` never cleans this persisted stderr.
        """
        subtype = wrapper.get("subtype")
        annotation = "claude reported an error"
        if isinstance(subtype, str) and subtype:
            annotation += f" (subtype: {subtype})"
        return f"{stderr.rstrip()}\n{annotation}" if stderr.strip() else annotation
