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
import shutil

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
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=dict(invocation.env),
            )
        except FileNotFoundError as exc:
            # Defense-in-depth: _resolve_cli_path already gave a clean pre-spawn
            # missing-CLI error, but a TOCTOU race (the binary vanishing between
            # which and spawn) must still surface the actionable setup error, never
            # a raw FileNotFoundError escaping the Adapter.
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
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            # Defense-in-depth for the TOCTOU race (see invoke): the binary
            # disappearing between which and spawn still yields the setup error.
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
