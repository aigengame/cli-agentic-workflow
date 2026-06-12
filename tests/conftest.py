"""Shared fixtures for CLI-seam and run-directory-seam tests."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml


@pytest.fixture
def write_workflow_data(tmp_path: Path) -> Callable[[dict[str, Any]], Path]:
    """Return a factory that serializes a workflow mapping into tmp_path as YAML."""

    def _write(data: dict[str, Any]) -> Path:
        workflow_file = tmp_path / "workflow.yaml"
        workflow_file.write_text(yaml.safe_dump(data), encoding="utf-8")
        return workflow_file

    return _write


@pytest.fixture
def write_workflow(
    write_workflow_data: Callable[[dict[str, Any]], Path],
) -> Callable[[str], Path]:
    """Return a factory that writes a single shell-node workflow file into tmp_path."""

    def _write(command: str) -> Path:
        return write_workflow_data(
            {
                "name": "sample",
                "version": 1,
                "nodes": [{"id": "greet", "kind": "shell", "inputs": {"command": command}}],
            }
        )

    return _write
