"""The ``claude.print`` Adapter: invokes ``claude -p`` headless mode (#9).

This is the first real Adapter behind the vendor-neutral interface of ADR 0006:
it spawns Claude Code's print/headless mode (``claude -p``) and normalizes the
process into an :class:`AgentResult`. ALL ``claude``-specific knowledge — the
``-p`` flag, ``--output-format json``, ``--json-schema``, and the result-wrapper
shape — lives here and nowhere in the kernel, so the executor, State, and Events
stay vendor-neutral.
"""

import asyncio
import json

from caw.adapter import Adapter, AdapterError, AgentInvocation, AgentResult

# The CLI entrypoint this Adapter drives. Resolved on PATH at invoke time (lazily),
# never at construction, so a shell-only or offline Run never requires `claude`.
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


class ClaudePrintAdapter(Adapter):
    """Invokes ``claude -p`` (Claude Code headless mode) and normalizes its result."""

    async def invoke(self, invocation: AgentInvocation) -> AgentResult:
        # The prompt is positional; the node's `args` pass through verbatim —
        # caw owns no policy engine, so it neither interprets nor injects
        # sandbox/approval (or any) flags. exec (not shell): args are a list, so
        # there is no shell interpolation of the prompt or the passthrough flags.
        argv = [CLAUDE_CLI, "-p", invocation.prompt, *invocation.args]
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
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=dict(invocation.env),
            )
        except FileNotFoundError as exc:
            raise AdapterError(f"{_node_context(invocation)}: {_MISSING_CLI_HINT}") from exc
        stdout_bytes, stderr_bytes = await process.communicate()
        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        exit_status = process.returncode if process.returncode is not None else -1
        # Parse structured output only on a successful, structured-requested run:
        # a non-zero exit is already a node failure (ADR 0006), so unparseable
        # stdout from a failed process is moot and must NOT mask the real exit.
        structured_output: object | None = None
        if wants_structured and exit_status == 0:
            structured_output = self._extract_structured_output(stdout, invocation)
        return AgentResult(
            exit_status=exit_status,
            stdout=stdout,
            stderr=stderr,
            structured_output=structured_output,
        )

    async def capability_check(self) -> str:
        """Probe the installed ``claude`` CLI and return its version string.

        This is adapter INFRASTRUCTURE, not a node invocation: it probes
        ``claude --version`` using the ambient environment to locate and run the
        CLI (no node-declared env allow-list applies, unlike :meth:`invoke`). The
        version is returned to the caller and kept adapter-local — it is NOT
        persisted to State (token/cost/version surfacing is carved out to #79). A
        missing CLI surfaces the same actionable setup AdapterError as invoke.
        """
        try:
            process = await asyncio.create_subprocess_exec(
                CLAUDE_CLI,
                "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise AdapterError(f"capability check: {_MISSING_CLI_HINT}") from exc
        stdout_bytes, stderr_bytes = await process.communicate()
        if process.returncode != 0:
            stderr = stderr_bytes.decode("utf-8", errors="replace").strip()
            raise AdapterError(
                f"capability check: 'claude --version' exited "
                f"{process.returncode}: {stderr or '<no stderr>'}"
            )
        return stdout_bytes.decode("utf-8", errors="replace").strip()

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
    def _extract_structured_output(stdout: str, invocation: AgentInvocation) -> object | None:
        """Parse the CLI's JSON result wrapper and return its `structured_output`.

        The CLI prints a single JSON object whose top-level `structured_output`
        field holds the schema-shaped value (separate from the freeform `result`
        text). Unparseable stdout when structured output was REQUIRED means the
        adapter cannot produce a result — an AdapterError, per ADR 0006. The value
        is returned as-is (including JSON null); the kernel is the sole arbiter of
        whether it satisfies the Output Contract.
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
        return wrapper.get("structured_output")
