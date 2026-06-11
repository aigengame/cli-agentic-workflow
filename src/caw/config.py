"""Parse workflow definition files into raw configuration data."""

from pathlib import Path
from typing import Any

import yaml


class WorkflowConfigError(Exception):
    """Raised when a workflow definition file cannot be read or parsed."""


def load_workflow_file(path: Path) -> dict[str, Any]:
    """Read a YAML workflow definition file and return its raw mapping."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise WorkflowConfigError(f"cannot read workflow file {path}: {exc}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise WorkflowConfigError(f"invalid YAML in workflow file {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise WorkflowConfigError(f"workflow file {path} must contain a YAML mapping")
    return data
