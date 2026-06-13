"""The ``claude.print`` Adapter: invokes ``claude -p`` headless mode (#9).

This is the first real Adapter behind the vendor-neutral interface of ADR 0006:
it spawns Claude Code's print/headless mode (``claude -p``) and normalizes the
process into an :class:`AgentResult`. ALL ``claude``-specific knowledge — the
``-p`` flag, ``--output-format json``, ``--json-schema``, and the result-wrapper
shape — lives here and nowhere in the kernel, so the executor, State, and Events
stay vendor-neutral.
"""

import asyncio

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


class ClaudePrintAdapter(Adapter):
    """Invokes ``claude -p`` (Claude Code headless mode) and normalizes its result."""

    async def invoke(self, invocation: AgentInvocation) -> AgentResult:
        # The prompt is positional; the node's `args` pass through verbatim —
        # caw owns no policy engine, so it neither interprets nor injects
        # sandbox/approval (or any) flags. exec (not shell): args are a list, so
        # there is no shell interpolation of the prompt or the passthrough flags.
        argv = [CLAUDE_CLI, "-p", invocation.prompt, *invocation.args]
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
            raise AdapterError(
                f"node {invocation.node_id!r} (adapter {invocation.adapter!r}): "
                f"{_MISSING_CLI_HINT}"
            ) from exc
        stdout_bytes, stderr_bytes = await process.communicate()
        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        return AgentResult(
            exit_status=process.returncode if process.returncode is not None else -1,
            stdout=stdout,
            stderr=stderr,
        )
