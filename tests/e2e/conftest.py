"""Fixtures and collection hooks for the real-agent-CLI e2e suite (#86).

Every test collected under ``tests/e2e/`` is auto-tagged with the ``e2e`` marker by
location, so the whole tier is selectable as ``pytest -m e2e`` and excluded from CI's
``pytest -m "not e2e"`` without decorating each test. The :func:`agent` fixture
resolves the single selected agent and FAILS (never skips) when its CLI is absent.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import pytest

from e2e import harness

_E2E_DIR = Path(__file__).resolve().parent


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items: Iterable[pytest.Item]) -> None:
    """Auto-mark every test under ``tests/e2e/`` with the ``e2e`` marker (#86).

    A sub-directory conftest's ``pytest_collection_modifyitems`` receives the WHOLE
    session's item list, so this filters by location and marks only the e2e subtree —
    never the non-e2e tiers. ``tryfirst`` runs this before pytest's own ``-m`` / ``-k``
    deselection, so the dynamic marker is in place when the suite split is applied.
    """
    for item in items:
        item_path = Path(item.path).resolve()
        if item_path == _E2E_DIR or _E2E_DIR in item_path.parents:
            item.add_marker(pytest.mark.e2e)


@pytest.fixture
def agent() -> str:
    """The single agent this e2e session drives (``CAW_E2E_AGENT``, default claude).

    The CLI presence check is NOT done here: each test calls
    :func:`harness.require_agent_cli` in its body so a missing CLI is reported as a
    test FAILURE (not a setup ERROR), and so the real-failure test cannot silently
    pass when the CLI is simply absent (a missing CLI must fail it, not satisfy its
    "the run failed" expectation). See the test module for the per-test guard.
    """
    return harness.selected_agent()
