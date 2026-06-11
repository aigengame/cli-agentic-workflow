"""Shared fixtures for CLI-seam and run-directory-seam tests."""

from collections.abc import Callable
from pathlib import Path

import pytest


@pytest.fixture
def write_workflow(tmp_path: Path) -> Callable[[str], Path]:
    """Return a factory that writes a single shell-node workflow file into tmp_path."""

    def _write(command: str) -> Path:
        workflow_file = tmp_path / "workflow.yaml"
        workflow_file.write_text(
            "name: sample\n"
            "version: 1\n"
            "nodes:\n"
            "  - id: greet\n"
            "    kind: shell\n"
            "    inputs:\n"
            f"      command: {command!r}\n",
            encoding="utf-8",
        )
        return workflow_file

    return _write
