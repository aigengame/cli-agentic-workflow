"""Shared harness for the real-agent-CLI e2e suite (#86).

This module holds the e2e suite's pure, deterministic infrastructure so it can be
unit-tested OFFLINE (see ``tests/test_e2e_harness.py``, which runs in CI) while the
real-CLI tests under ``tests/e2e/`` exercise it against a live agent:

* **Agent selection** — exactly ONE agent runs per e2e session, chosen by
  ``CAW_E2E_AGENT`` (default ``claude``). Both ``claude`` and ``codex`` are wired,
  each one entry in :data:`_AGENTS`.
* **skip = fail** — :func:`require_agent_cli` FAILS (never skips) when the selected
  agent's CLI is absent, so a missing CLI is loud, not silent green.
* **Transient-only bounded retry** — :func:`run_with_transient_retry` re-runs ONLY
  on transient failures (network / 5xx / rate-limit), detected by
  :func:`is_transient_failure`. Assertions live OUTSIDE the retry loop in each test,
  so an assertion/contract failure is never retried.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import pytest

from caw.executor import RunResult
from caw.state import StateStore


class E2EConfigError(Exception):
    """The e2e suite is misconfigured: an unsupported ``CAW_E2E_AGENT`` was selected."""


@dataclass(frozen=True)
class AgentSpec:
    """How a selected agent maps onto the kernel: its Adapter name and its CLI binary."""

    adapter: str
    cli: str


# The single source of truth for which agents the e2e suite can drive. Keyed by the
# value of CAW_E2E_AGENT. Adding an agent is a one-line entry here — the agent
# selector, CLI presence check, and adapter resolution all read from this map.
_AGENTS: dict[str, AgentSpec] = {
    "claude": AgentSpec(adapter="claude.print", cli="claude"),
    "codex": AgentSpec(adapter="codex.exec", cli="codex"),
}

# The agent exercised when CAW_E2E_AGENT is unset (decision #3).
DEFAULT_E2E_AGENT = "claude"

# Up to two reruns on transient failures (decision #6: "1-2 reruns"). Three total
# attempts means an assertion/contract failure (never retried — it lives outside the
# loop) is reported on the first run, while a flaky network/5xx/rate-limit blip gets
# a bounded second and third chance.
DEFAULT_MAX_ATTEMPTS = 3

# Substrings (matched case-insensitively against a failed Node's stderr) that mark a
# TRANSIENT failure: a network blip, a 5xx, or a rate limit. These — and ONLY these —
# are retried. A missing/unauthenticated CLI, a bad flag, an Output-Contract breach,
# or any assertion is NOT in this set, so it fails fast without burning reruns. The
# Anthropic API surfaces overload as HTTP 529, included here alongside the standard
# 5xx codes.
_TRANSIENT_MARKERS: tuple[str, ...] = (
    "rate limit",
    "rate_limit",
    "ratelimit",
    "too many requests",
    "429",
    "500 internal",
    "internal server error",
    "502",
    "bad gateway",
    "503",
    "service unavailable",
    "504",
    "gateway timeout",
    "529",
    "overloaded",
    "temporarily unavailable",
    "connection reset",
    "connection refused",
    "connection aborted",
    "connection error",
    "econnreset",
    "econnrefused",
    "etimedout",
    "socket hang up",
    "fetch failed",
    "network error",
)


def selected_agent() -> str:
    """The agent this e2e session drives: ``CAW_E2E_AGENT`` or the default (#3)."""
    return os.environ.get("CAW_E2E_AGENT") or DEFAULT_E2E_AGENT


def _spec(agent: str) -> AgentSpec:
    try:
        return _AGENTS[agent]
    except KeyError as exc:
        supported = ", ".join(sorted(_AGENTS))
        raise E2EConfigError(
            f"unsupported CAW_E2E_AGENT={agent!r}; supported agents: {supported}"
        ) from exc


def adapter_for_agent(agent: str) -> str:
    """The Adapter name that drives ``agent``'s real CLI (e.g. ``claude`` -> ``claude.print``)."""
    return _spec(agent).adapter


def agent_cli_name(agent: str) -> str:
    """The CLI binary name ``agent`` resolves on PATH (e.g. ``claude`` -> ``claude``)."""
    return _spec(agent).cli


def require_agent_cli(agent: str) -> str:
    """Return the resolved CLI path for ``agent``, or FAIL the test if it is absent.

    This is the skip = fail rule (decision #2): when the selected agent's CLI is not
    on PATH the e2e test FAILS — it never skips — so a missing CLI surfaces loudly
    instead of as silent green. ``pytest.fail(pytrace=False)`` reports a clean,
    actionable failure rather than a traceback. (An unauthenticated CLI is not
    cheaply pre-detectable; it surfaces as the real run failing the test's
    assertions, which is equally a failure.)
    """
    cli = agent_cli_name(agent)
    resolved = shutil.which(cli)
    if resolved is None:
        pytest.fail(
            f"e2e: the {cli!r} CLI for CAW_E2E_AGENT={agent!r} is not on PATH. "
            "Agent e2e tests FAIL (never skip) when the selected agent CLI is "
            "unavailable (#86). Install/authenticate it, select another agent via "
            "CAW_E2E_AGENT, or run the non-e2e suite with `pytest -m 'not e2e'`.",
            pytrace=False,
        )
    return resolved


def agent_env_names() -> tuple[str, ...]:
    """The env-var NAMES an e2e agent Node should declare so the real CLI can run.

    The kernel's env policy is allow-list-only: a Node's process receives a variable
    solely if the Node declared its NAME and it is present in the parent environment
    (ADR 0006). A real ``claude`` needs its ambient auth/config (HOME, the keychain
    session, ANTHROPIC_*/CLAUDE_* vars, PATH for any tools it spawns), so an e2e Node
    declares every name currently in the environment — this is a local developer run
    against their own authenticated CLI, where passing the ambient environment is the
    point. The offline mock suite is what pins the strict no-leak env policy.
    """
    return tuple(os.environ)


def is_transient_text(text: str) -> bool:
    """Whether ``text`` carries a transient-failure marker (network / 5xx / rate-limit)."""
    lowered = text.lower()
    return any(marker in lowered for marker in _TRANSIENT_MARKERS)


def is_transient_failure(result: RunResult) -> bool:
    """Whether ``result`` failed for a TRANSIENT reason (network / 5xx / rate-limit).

    A succeeded Run is never transient. Otherwise the failed Nodes' stderr is scanned
    for the transient markers; ONLY a match is retryable. Deterministic failures — a
    bad flag, an Output-Contract breach, a missing/unauthenticated CLI — carry none of
    these markers and so are not retried (decision #6).
    """
    if result.succeeded:
        return False
    failed_stderr = "\n".join(node.stderr for node in result.node_results if not node.succeeded)
    return is_transient_text(failed_stderr)


async def run_with_transient_retry(
    run: Callable[[], Awaitable[RunResult]],
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> RunResult:
    """Invoke ``run`` up to ``max_attempts`` times, retrying ONLY on transient failure.

    Returns the first non-transient outcome (success OR a deterministic failure), or
    the last result once the attempt budget is exhausted. Exceptions raised by ``run``
    propagate immediately and are NOT retried (a crash is not a transient agent blip).
    Crucially, a test calls this and then asserts on the returned result, so an
    assertion/contract failure — which happens AFTER this returns — is never retried
    (decision #6).
    """
    result = await run()
    attempts = 1
    while attempts < max_attempts and is_transient_failure(result):
        result = await run()
        attempts += 1
    return result


class CliResult(Protocol):
    """The slice of a Typer/Click invocation result the CLI retry helper needs.

    ``output`` is a read-only property on the concrete Click/Typer result, so it is
    declared as one here for structural compatibility.
    """

    exit_code: int

    @property
    def output(self) -> str: ...


def latest_run_dir(runs_root: Path) -> Path | None:
    """The most recently written run directory under ``runs_root``, or None if none.

    ``caw run`` materializes a NEW run dir per invocation, so a retried CLI e2e leaves
    several; assertions must target the latest attempt. Chosen by mtime, which is robust
    even when two run ids share a same-second timestamp prefix.
    """
    if not runs_root.exists():
        return None
    dirs = [path for path in runs_root.iterdir() if path.is_dir()]
    return max(dirs, key=lambda path: path.stat().st_mtime) if dirs else None


def cli_run_is_transient(run_dir: Path) -> bool:
    """Whether the Run persisted in ``run_dir`` failed for a TRANSIENT reason.

    The CLI returns only an exit code, so transient detection reads the Run's State: a
    failed / errored / timed_out Node whose persisted stderr carries a transient marker
    (network / 5xx / rate-limit). A deterministic failure — a bad flag, a plain non-zero
    exit, a node timeout — carries none and is not retried, the same policy as
    :func:`is_transient_failure` applies on the in-process seam.
    """
    state_path = run_dir / "state.sqlite"
    if not state_path.exists():
        return False
    run_id = run_dir.name
    stderrs: list[str] = []
    with StateStore(state_path) as state:
        for node_id, status in state.node_statuses(run_id).items():
            if status not in {"failed", "errored", "timed_out"}:
                continue
            output = state.node_output(run_id, node_id)
            value = output.get("stderr") if output is not None else None
            if isinstance(value, str):
                stderrs.append(value)
    return any(is_transient_text(text) for text in stderrs)


def run_cli_with_transient_retry(
    invoke: Callable[[], CliResult],
    runs_root: Path,
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> tuple[CliResult, Path | None]:
    """Invoke a ``caw`` CLI command, retrying ONLY on a transient Run failure.

    The CLI-seam analogue of :func:`run_with_transient_retry` (decision #6): each
    ``invoke`` runs the command (``caw run`` writes a fresh run dir), and a non-zero
    exit is retried only when the latest Run's State shows a transient failure
    (:func:`cli_run_is_transient`). Returns the final result and the latest run dir —
    the attempt to assert on, which resolves the multiple-run-dir a retry creates.
    Assertion/contract checks live in the caller, AFTER this returns, so they are never
    retried.
    """
    result = invoke()
    attempts = 1
    while attempts < max_attempts and result.exit_code != 0:
        run_dir = latest_run_dir(runs_root)
        if run_dir is None or not cli_run_is_transient(run_dir):
            break
        result = invoke()
        attempts += 1
    return result, latest_run_dir(runs_root)
