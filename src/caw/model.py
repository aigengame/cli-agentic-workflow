"""The minimal Workflow IR: typed and immutable once a Run starts (ADR 0002)."""

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from caw.config import WorkflowConfigError


def _require_non_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be blank or whitespace-only")
    return value


class ShellNodeInputs(BaseModel):
    """Inputs of a shell Node."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command: str

    _command_non_blank = field_validator("command")(_require_non_blank)


class Node(BaseModel):
    """A unit of work in a Workflow; the walking skeleton supports only shell Nodes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    kind: Literal["shell"]
    inputs: ShellNodeInputs

    _id_non_blank = field_validator("id")(_require_non_blank)


class Workflow(BaseModel):
    """A normalized Workflow IR for one Run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    version: int
    nodes: tuple[Node, ...]

    _name_non_blank = field_validator("name")(_require_non_blank)

    @field_validator("nodes")
    @classmethod
    def _node_ids_must_be_unique(cls, nodes: tuple[Node, ...]) -> tuple[Node, ...]:
        if not nodes:
            raise ValueError("nodes must not be empty")
        seen: set[str] = set()
        for node in nodes:
            if node.id in seen:
                raise ValueError(f"duplicate node id {node.id!r}")
            seen.add(node.id)
        return nodes


def _first_error_line(exc: ValidationError) -> str:
    first = exc.errors()[0]
    location = ".".join(str(part) for part in first["loc"]) or "workflow"
    remainder = exc.error_count() - 1
    suffix = f" (+{remainder} more)" if remainder else ""
    return f"{location}: {first['msg']}{suffix}"


def normalize_workflow(raw: dict[str, Any], source: str) -> Workflow:
    """Normalize a raw workflow mapping into the Workflow IR, or fail with field paths."""
    try:
        return Workflow.model_validate(raw)
    except ValidationError as exc:
        raise WorkflowConfigError(
            f"invalid workflow definition in {source}: {_first_error_line(exc)}"
        ) from exc


def definition_checksum(workflow: Workflow) -> str:
    """Checksum of the normalized workflow definition, over its canonical JSON form."""
    canonical = json.dumps(
        workflow.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def workflow_snapshot(workflow: Workflow) -> dict[str, Any]:
    """The normalized workflow snapshot persisted in the run directory."""
    return {
        "definition_checksum": definition_checksum(workflow),
        "workflow": workflow.model_dump(mode="json"),
    }
