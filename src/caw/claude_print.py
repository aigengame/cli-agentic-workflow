"""The ``claude.print`` Adapter: invokes ``claude -p`` headless mode (#9).

This is the first real Adapter behind the vendor-neutral interface of ADR 0006:
it spawns Claude Code's print/headless mode (``claude -p``) and normalizes the
process into an :class:`AgentResult`. ALL ``claude``-specific knowledge — the
``-p`` flag, ``--output-format json``, ``--json-schema``, and the result-wrapper
shape — lives here and nowhere in the kernel, so the executor, State, and Events
stay vendor-neutral.
"""

import asyncio

from caw.adapter import Adapter, AgentInvocation, AgentResult

# The CLI entrypoint this Adapter drives. Resolved on PATH at invoke time (lazily),
# never at construction, so a shell-only or offline Run never requires `claude`.
CLAUDE_CLI = "claude"


class ClaudePrintAdapter(Adapter):
    """Invokes ``claude -p`` (Claude Code headless mode) and normalizes its result."""

    async def invoke(self, invocation: AgentInvocation) -> AgentResult:
        argv = [CLAUDE_CLI, "-p", invocation.prompt, *invocation.args]
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await process.communicate()
        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        return AgentResult(
            exit_status=process.returncode if process.returncode is not None else -1,
            stdout=stdout,
            stderr=stderr,
        )
