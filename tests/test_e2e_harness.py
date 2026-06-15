"""Offline unit tests for the e2e harness (#86).

The e2e suite's infrastructure — agent selection, the skip = fail CLI check, the
transient-failure classifier, and the bounded-retry helper — is pure and
deterministic, so it is unit-tested HERE (a non-e2e module that runs in CI) rather
than against a live agent. The real-CLI tests under ``tests/e2e/`` then rely on this
harness to drive exactly one selected agent locally.

This module also imports ``e2e.harness`` to pin that the harness package is
importable from the non-e2e suite (the ``tests/`` dir is on ``sys.path`` via the
root ``conftest.py``; ``tests/e2e/__init__.py`` makes ``e2e`` a package).
"""

from __future__ import annotations

import shutil
import tomllib
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
from e2e import harness

from caw.executor import FAILED, NodeResult, RunResult

RunFactory = Callable[[], Awaitable[RunResult]]
REPO_ROOT = Path(__file__).resolve().parent.parent


def _node(node_id: str, *, stderr: str = "", failed: bool = False) -> NodeResult:
    """A minimal NodeResult for transient/retry tests."""
    return NodeResult(
        node_id=node_id,
        exit_status=1 if failed else 0,
        stdout="",
        stderr=stderr,
        started_at="t0",
        finished_at="t1",
        failure_kind=FAILED if failed else None,
    )


def _run(*nodes: NodeResult) -> RunResult:
    return RunResult(run_id="r", node_results=tuple(nodes))


def _ok_run() -> RunResult:
    return _run(_node("n"))


def _failed_run(stderr: str) -> RunResult:
    return _run(_node("n", stderr=stderr, failed=True))


def _counting_factory(results: list[RunResult]) -> tuple[RunFactory, list[int]]:
    """An async run factory that yields ``results`` in order, recording its call count."""
    calls = [0]

    async def run() -> RunResult:
        index = calls[0]
        calls[0] += 1
        return results[index]

    return run, calls


# --- Agent selection (decision #3) -----------------------------------------------


def test_selected_agent_defaults_to_claude(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CAW_E2E_AGENT", raising=False)

    assert harness.selected_agent() == "claude"


def test_selected_agent_reads_the_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAW_E2E_AGENT", "codex")

    assert harness.selected_agent() == "codex"


def test_blank_env_var_falls_back_to_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    # An empty CAW_E2E_AGENT must not select the "" agent; it falls back to the default.
    monkeypatch.setenv("CAW_E2E_AGENT", "")

    assert harness.selected_agent() == harness.DEFAULT_E2E_AGENT


def test_claude_maps_to_the_claude_print_adapter() -> None:
    assert harness.adapter_for_agent("claude") == "claude.print"


def test_claude_resolves_the_claude_cli_binary() -> None:
    assert harness.agent_cli_name("claude") == "claude"


def test_an_unsupported_agent_is_a_config_error() -> None:
    # codex is not wired until #11; selecting it (or any unknown agent) is a config
    # error, distinct from a missing CLI — it names the supported agents.
    with pytest.raises(harness.E2EConfigError) as excinfo:
        harness.adapter_for_agent("codex")

    assert "codex" in str(excinfo.value)
    assert "claude" in str(excinfo.value), "the error names the supported agents"


# --- skip = fail: missing CLI fails, never skips (decision #2) --------------------


def test_require_agent_cli_returns_the_resolved_path_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: f"/abs/bin/{name}")

    assert harness.require_agent_cli("claude") == "/abs/bin/claude"


def test_require_agent_cli_FAILS_not_skips_when_the_cli_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The crux of #86: a missing selected-agent CLI must FAIL the test (pytest.fail),
    # never skip it — so it can never read as silent green. pytest.fail raises Failed
    # (pytest.fail.Exception), which is NOT pytest.skip.Exception.
    monkeypatch.setattr(shutil, "which", lambda name: None)

    with pytest.raises(pytest.fail.Exception) as excinfo:
        harness.require_agent_cli("claude")

    assert not isinstance(excinfo.value, pytest.skip.Exception), "it must fail, not skip"
    message = str(excinfo.value)
    assert "claude" in message
    assert "not on PATH" in message


# --- Transient classification (decision #6) --------------------------------------


def test_a_succeeded_run_is_never_transient() -> None:
    assert harness.is_transient_failure(_ok_run()) is False


@pytest.mark.parametrize(
    "stderr",
    [
        "API Error: 429 rate limit exceeded",
        "Error: 503 Service Unavailable",
        "anthropic api 529 overloaded, retry later",
        "request failed: ECONNRESET",
        "fetch failed: socket hang up",
    ],
)
def test_a_network_5xx_or_rate_limit_failure_is_transient(stderr: str) -> None:
    assert harness.is_transient_failure(_failed_run(stderr)) is True


@pytest.mark.parametrize(
    "stderr",
    [
        "error: unknown option '--caw-e2e-nonexistent-flag'",
        "the 'claude' CLI was not found on PATH",
        "Invalid API key; please run `claude login`",
        "output did not satisfy the Output Contract: 'name' is a required property",
        "AssertionError: expected 4",
    ],
)
def test_a_deterministic_failure_is_not_transient(stderr: str) -> None:
    # A bad flag, a missing/unauthenticated CLI, an Output-Contract breach, or an
    # assertion must NOT be retried — none carry a transient marker.
    assert harness.is_transient_failure(_failed_run(stderr)) is False


def test_only_failed_nodes_stderr_is_scanned() -> None:
    # A transient-looking string on a SUCCEEDED node must not make the Run transient;
    # only failed Nodes' stderr is inspected.
    run = _run(
        _node("ok", stderr="mentions 503 but succeeded"),
        _node("fail", stderr="boom", failed=True),
    )

    assert harness.is_transient_failure(run) is False


# --- Bounded transient retry (decision #6) ---------------------------------------


@pytest.mark.asyncio
async def test_a_transient_failure_is_retried_until_it_succeeds() -> None:
    run, calls = _counting_factory(
        [_failed_run("503 Service Unavailable"), _failed_run("429 rate limit"), _ok_run()]
    )

    result = await harness.run_with_transient_retry(run, max_attempts=3)

    assert result.succeeded
    assert calls[0] == 3, "it retried the two transient failures, then succeeded"


@pytest.mark.asyncio
async def test_a_deterministic_failure_is_returned_without_retry() -> None:
    run, calls = _counting_factory([_failed_run("error: unknown option"), _ok_run()])

    result = await harness.run_with_transient_retry(run, max_attempts=3)

    assert not result.succeeded, "the deterministic failure is returned as-is"
    assert calls[0] == 1, "a non-transient failure is never retried"


@pytest.mark.asyncio
async def test_retries_are_bounded_by_max_attempts() -> None:
    run, calls = _counting_factory([_failed_run("503") for _ in range(5)])

    result = await harness.run_with_transient_retry(run, max_attempts=3)

    assert not result.succeeded, "the last (still-transient) result is returned"
    assert calls[0] == 3, "retries stop at max_attempts even if still transient"


@pytest.mark.asyncio
async def test_an_exception_in_the_run_is_not_retried() -> None:
    # A crash inside the run (not a transient agent blip) propagates immediately and
    # is never retried.
    calls = [0]

    async def run() -> RunResult:
        calls[0] += 1
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await harness.run_with_transient_retry(run, max_attempts=3)

    assert calls[0] == 1, "an exception is not retried"


# --- Env declaration for real agent Nodes (ADR 0006 allow-list) ------------------


def test_agent_env_names_declares_present_environment_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An e2e agent Node declares the env-var NAMES the real CLI needs; the kernel
    # resolves their values from the parent environment at run time.
    monkeypatch.setenv("CAW_E2E_PROBE_VAR", "x")

    assert "CAW_E2E_PROBE_VAR" in harness.agent_env_names()


# --- Suite split: the e2e marker is registered (acceptance criterion #1) ----------


def _pytest_ini_options() -> dict[str, object]:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    options = pyproject["tool"]["pytest"]["ini_options"]
    assert isinstance(options, dict)
    return options


def test_e2e_marker_is_registered() -> None:
    # The suite split (criterion #1) needs the `e2e` marker declared so it is
    # selectable as `pytest -m e2e` / excludable as `pytest -m "not e2e"` and is not
    # treated as an unknown marker under --strict-markers.
    markers = _pytest_ini_options().get("markers", [])
    assert isinstance(markers, list)
    assert any(str(marker).startswith("e2e:") for marker in markers), (
        "an `e2e:` marker must be registered in [tool.pytest.ini_options].markers"
    )


def test_strict_markers_is_enabled() -> None:
    # --strict-markers turns a typo'd or unregistered marker into an error, so the
    # e2e split can never silently mis-tag a test.
    addopts = _pytest_ini_options().get("addopts", "")
    rendered = " ".join(addopts) if isinstance(addopts, list) else str(addopts)
    assert "--strict-markers" in rendered
