"""Parse workflow definition files into raw configuration data."""

from collections.abc import Hashable
from pathlib import Path
from typing import Any

import yaml


class WorkflowConfigError(Exception):
    """Raised when a workflow definition file cannot be read or parsed."""


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """A SafeLoader that rejects duplicate mapping keys instead of keeping the last one."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        seen: set[Any] = set()
        for key_node, _ in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge":
                # `<<` merge keys are expanded by the base loader's flatten_mapping,
                # and an explicit key legally overrides a merged one — neither is a
                # duplicate-key authoring error.
                continue
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, Hashable):
                # Let the base loader report unhashable keys as a ConstructorError.
                continue
            if key in seen:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    f"found duplicate key {key!r}",
                    key_node.start_mark,
                )
            seen.add(key)
        return super().construct_mapping(node, deep)


def load_workflow_file(path: Path) -> dict[str, Any]:
    """Read a YAML workflow definition file and return its raw mapping."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise WorkflowConfigError(f"cannot read workflow file {path}: {exc}") from exc
    try:
        data = yaml.load(text, Loader=_UniqueKeySafeLoader)
    except yaml.YAMLError as exc:
        raise WorkflowConfigError(f"invalid YAML in workflow file {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise WorkflowConfigError(f"workflow file {path} must contain a YAML mapping")
    return data
