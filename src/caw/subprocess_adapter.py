"""Shared subprocess-Adapter infrastructure (#83).

The per-CLI subprocess machinery `claude.print` introduced (#9) is generic: every
real Agent CLI Adapter — `claude -p` (#9), `codex exec` (#11) — must locate its CLI
on PATH, spawn the absolute path with the strict node env allow-list, isolate stdin,
own a process group so the whole tree is killable on timeout/cancellation, reap it,
and turn a missing CLI into one actionable :class:`AdapterError`. This module owns
that machinery ONCE so a second Adapter reuses it instead of re-implementing the
identical dance, and a cross-cutting fix (a process-lifecycle bug, an env-policy
change) lands in a single place.

:class:`SubprocessAdapter` is the base a real CLI Adapter subclasses, supplying its
CLI binary name and missing-CLI hint; it inherits :meth:`SubprocessAdapter.run_cli`
(locate + spawn + communicate-or-kill + decode + returncode pass-through). The
free helpers — :func:`node_context` (the ``node 'id' (adapter 'name')`` diagnostic
prefix), :func:`parse_json_object`, and :func:`read_json_object` (the
read -> ``json.loads`` -> dict :class:`AdapterError` ladder) — are shared by both the
real Adapters and :class:`caw.adapter.MockAdapter`, so the same malformed-JSON error
surfaces with the same shape everywhere.
"""

import asyncio
import contextlib
import json
import os
import shutil
import signal
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from caw.adapter import AdapterError, AgentInvocation


def node_context(invocation: AgentInvocation) -> str:
    """The ``node 'id' (adapter 'name')`` prefix an Adapter's error messages carry.

    Consolidated here (#83) so every subprocess Adapter — claude (#9), codex (#11) —
    and its diagnostics name the failing node and adapter identically, rather than
    each re-deriving the prefix.
    """
    return f"node {invocation.node_id!r} (adapter {invocation.adapter!r})"


def parse_json_object(text: str, context: str, source_label: str) -> dict[str, object]:
    """Parse ``text`` as a single JSON object, or raise an :class:`AdapterError`.

    The ``json.loads`` -> ``isinstance dict`` half of the shared JSON ladder (#83),
    used where the JSON arrives as a STRING (an Agent CLI's stdout wrapper). The
    error names ``context`` (the node/adapter) and ``source_label`` (what produced
    the text, e.g. ``"'claude -p --output-format json'"``) so an unparseable or
    non-object payload is a node-level AdapterError, never a raw decode error that
    escapes the Adapter.
    """
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AdapterError(
            f"{context}: expected a JSON result from {source_label} but could not "
            f"parse stdout: {exc}"
        ) from exc
    if not isinstance(parsed, dict):
        raise AdapterError(
            f"{context}: expected a JSON object from {source_label}, got {type(parsed).__name__}"
        )
    return parsed


def read_json_object(path: Path, context: str, source_label: str) -> dict[str, object]:
    """Read ``path`` and parse it as a single JSON object, or raise an AdapterError.

    The read -> ``json.loads`` -> ``isinstance dict`` whole of the shared JSON ladder
    (#83), used where the JSON arrives as a FILE (a mock Adapter's fixture). An
    unreadable file, invalid JSON, or a non-object payload is a node-level
    :class:`AdapterError` naming ``context`` and the ``source_label`` (e.g.
    ``"fixture"``), so the same malformed-JSON failure surfaces the same way the
    string path does.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AdapterError(f"{context}: cannot read {source_label} {path}: {exc}") from exc
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AdapterError(f"{context}: invalid JSON {source_label} {path}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise AdapterError(f"{context}: {source_label} {path} must be a JSON object")
    return parsed


@dataclass(frozen=True)
class CompletedSubprocess:
    """The normalized outcome of one spawned Agent-CLI subprocess.

    ``returncode`` is the process's REAL exit status (an int — ``communicate`` always
    settles it), including a NEGATIVE signal-kill code (e.g. ``-9``) passed through
    as-is rather than coerced to the executor's ``-1`` TIMED_OUT sentinel (#84).
    ``stdout`` / ``stderr`` are decoded with ``backslashreplace`` so an undecodable
    byte survives recoverably (matching the executor's shell-node decode), since they
    feed State and downstream ``when`` predicates.
    """

    returncode: int
    stdout: str
    stderr: str
    artifacts: tuple[Path, ...] = ()


_ARTIFACT_SCAN_EXCLUDED_DIRS = frozenset(
    {".git", ".caw", ".venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
)


def _artifact_fingerprint(path: Path) -> tuple[int, int] | None:
    """Return a cheap file-change fingerprint, or None when the path is not a file."""
    try:
        stat = path.stat()
    except OSError:
        return None
    if not path.is_file():
        return None
    return (stat.st_mtime_ns, stat.st_size)


def _artifact_snapshot(root: Path) -> dict[Path, tuple[int, int]]:
    """Snapshot regular files under ``root`` for real-CLI artifact detection (#16)."""
    snapshot: dict[Path, tuple[int, int]] = {}
    for directory, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in _ARTIFACT_SCAN_EXCLUDED_DIRS]
        current = Path(directory)
        for filename in filenames:
            path = current / filename
            fingerprint = _artifact_fingerprint(path)
            if fingerprint is not None:
                snapshot[path] = fingerprint
    return snapshot


def _changed_artifacts(root: Path, before: Mapping[Path, tuple[int, int]]) -> tuple[Path, ...]:
    """Files under ``root`` that were created or modified since ``before``."""
    after = _artifact_snapshot(root)
    return tuple(
        sorted(path for path, fingerprint in after.items() if before.get(path) != fingerprint)
    )


async def _communicate_or_kill(
    process: "asyncio.subprocess.Process",
) -> tuple[bytes, bytes]:
    """``communicate`` on ``process``, killing+reaping its tree on cancellation/timeout.

    The kernel wraps ``invoke`` in ``asyncio.timeout``; when the budget expires the
    awaited ``communicate`` is cancelled (``CancelledError``) — and a bare
    ``TimeoutError`` can surface the same way — leaving the spawned CLI (and any
    grandchild it launched) running. We catch ``BaseException`` (covers both), kill
    the WHOLE process tree by group and reap it so no orphan is left, then re-raise so
    the executor still classifies the node correctly.

    The process is spawned with ``start_new_session=True`` so the whole tree shares a
    process group that ``os.killpg`` signals; a grandchild's inherited stdout pipe
    would otherwise keep this call blocked for its full lifetime. The leader's
    ``returncode`` being set does NOT mean the process GROUP is dead — the direct CLI
    process can be reaped while a grandchild still holds the stdout/stderr pipe — so
    the group is signalled REGARDLESS of the leader's returncode. ``ProcessLookupError``
    is suppressed for the case where no group member remains.
    """
    try:
        return await process.communicate()
    except BaseException:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()
        raise


class SubprocessAdapter:
    """Base for a real Agent-CLI Adapter that spawns a CLI subprocess (#83).

    A subclass sets :attr:`cli_name` (the binary to locate on PATH) and
    :attr:`missing_cli_hint` (the actionable setup message a missing CLI surfaces),
    then builds its argv and calls :meth:`run_cli`. This base owns the cross-cutting
    machinery so claude (#9) and codex (#11) share it unchanged: locating the CLI via
    ``shutil.which`` (resolved ONCE per instance and cached), spawning the resolved
    ABSOLUTE path with a strict ``env`` (the node's allow-list), stdin isolation
    (``DEVNULL``), a private process group, process-group kill+reap on
    timeout/cancellation, returncode pass-through, recoverable decode, and the
    missing-CLI -> actionable :class:`AdapterError` (plus the TOCTOU
    ``FileNotFoundError`` fallback at spawn).

    It is NOT registered as an Adapter itself: it has no ``invoke`` — that, and all
    CLI-specific knowledge (flag names, output formats, result-wrapper shape), lives
    in the concrete subclass, keeping the vendor-neutral kernel boundary (ADR 0006).
    """

    # Subclass contract: the CLI binary name to locate on PATH and the actionable
    # message a missing CLI surfaces. Declared here (not set) so a subclass that
    # forgets them fails loudly at first use rather than silently mis-locating.
    cli_name: str
    missing_cli_hint: str

    def __init__(self) -> None:
        # The resolved absolute CLI path, located lazily on first use and cached for
        # the instance lifetime: a default-registry run constructs the Adapter with NO
        # side effects (no probe), so a shell-only/offline Run never requires the CLI,
        # while a Run that does use it resolves `which` ONCE instead of per invoke (#83).
        self._resolved_cli: str | None = None

    def resolve_cli_path(self, context_label: str) -> str:
        """Locate the CLI on the ambient PATH and return its absolute path (cached).

        Locating the tool is infrastructure: it uses the ambient environment (via
        ``shutil.which``), NOT a node's env allow-list. Returning an absolute path lets
        the caller spawn it with a strict ``env=`` (the node's allow-list) without
        needing ``PATH`` declared there and without leaking it into the child. Resolved
        ONCE and cached on the instance; a missing CLI is the Adapter being unable to
        produce a result at all, so it surfaces the actionable setup AdapterError BEFORE
        any spawn (#9). ``context_label`` names the caller (a node context, or
        ``"capability check"``) so the error is diagnosable.
        """
        if self._resolved_cli is None:
            resolved = shutil.which(self.cli_name)
            if resolved is None:
                raise AdapterError(f"{context_label}: {self.missing_cli_hint}")
            self._resolved_cli = resolved
        return self._resolved_cli

    async def run_cli(
        self,
        argv: list[str],
        *,
        context_label: str,
        env: Mapping[str, str] | None = None,
        capture_artifacts: bool = True,
        cwd: Path | None = None,
    ) -> CompletedSubprocess:
        """Spawn the CLI for ``argv`` and return its normalized completion.

        ``argv[0]`` must be the binary name a caller obtained from
        :meth:`resolve_cli_path`; this method spawns it with stdin isolated
        (``DEVNULL``), a private session/process group (so the whole tree is killable
        by group), ``env`` — the node's already-filtered allow-list for an
        ``invoke``, or ``None`` for an infrastructure probe that may use the ambient
        environment — and an optional node-owned ``cwd``. Artifact discovery scans
        that cwd when supplied, otherwise the ambient cwd used by direct calls.
        ``communicate`` is wrapped so a timeout/cancellation kills and reaps the
        tree, leaving no orphan, before re-raising. The returncode is passed through
        as a real int (a signal-kill stays negative, never the ``-1`` TIMED_OUT
        sentinel, #84) and stdout/stderr decode recoverably.
        """
        artifact_root = cwd if cwd is not None else Path.cwd()
        if cwd is not None:
            cwd.mkdir(parents=True, exist_ok=True)
        before = _artifact_snapshot(artifact_root) if capture_artifacts else {}
        try:
            if cwd is None:
                process = await asyncio.create_subprocess_exec(
                    *argv,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=dict(env) if env is not None else None,
                    start_new_session=True,
                )
            else:
                process = await asyncio.create_subprocess_exec(
                    *argv,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=dict(env) if env is not None else None,
                    cwd=str(cwd),
                    start_new_session=True,
                )
        except FileNotFoundError as exc:
            # Defense-in-depth: resolve_cli_path already gave a clean pre-spawn
            # missing-CLI error, but a TOCTOU race (the binary vanishing between
            # `which` and spawn) must still surface the actionable setup error, never
            # a raw FileNotFoundError escaping the Adapter.
            raise AdapterError(f"{context_label}: {self.missing_cli_hint}") from exc
        stdout_bytes, stderr_bytes = await _communicate_or_kill(process)
        # `communicate()` always settles the returncode, so it is a concrete int —
        # never None. Pass it through as-is, including a NEGATIVE signal-kill code.
        assert process.returncode is not None, "communicate() settles the returncode"
        return CompletedSubprocess(
            returncode=process.returncode,
            stdout=stdout_bytes.decode("utf-8", errors="backslashreplace"),
            stderr=stderr_bytes.decode("utf-8", errors="backslashreplace"),
            artifacts=(_changed_artifacts(artifact_root, before) if capture_artifacts else ()),
        )

    async def capability_check(self) -> str:
        """Probe the installed CLI and return its ``--version`` string (#83).

        Adapter INFRASTRUCTURE, not a node invocation: it locates the CLI on the
        ambient PATH (via :meth:`resolve_cli_path`, the same locate-and-error path
        :meth:`run_cli` callers use) and probes ``<cli> --version`` in the ambient
        environment — no node-declared env allow-list applies, so the spawn passes no
        ``env=``. The version is returned to the caller and kept adapter-local — it is
        NOT persisted to State (token/cost/version surfacing is carved out to #79). A
        missing CLI surfaces the same actionable setup AdapterError as an invoke,
        before any spawn.

        It lives on this base — shared by claude (#9) and codex (#11) — rather than on
        the abstract :class:`caw.adapter.Adapter`: the mock Adapter has no CLI to
        probe, so a no-op there would be dead. It has no production caller yet; the
        version is surfaced by #79's usage work and exercised by the real-CLI e2e (#86)
        today, so it is a real shared primitive, not a removable stub (#83 decision).
        """
        resolved_cli = self.resolve_cli_path("capability check")
        completed = await self.run_cli(
            [resolved_cli, "--version"],
            context_label="capability check",
            capture_artifacts=False,
        )
        if completed.returncode != 0:
            stderr = completed.stderr.strip()
            raise AdapterError(
                f"capability check: '{self.cli_name} --version' exited "
                f"{completed.returncode}: {stderr or '<no stderr>'}"
            )
        return completed.stdout.strip()
