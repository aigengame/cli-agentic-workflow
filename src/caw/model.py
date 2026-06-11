"""The minimal Workflow IR: typed and immutable once a Run starts (ADR 0002)."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from caw.config import WorkflowConfigError


class ShellNodeInputs(BaseModel):
    """Inputs of a shell Node."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command: str


class Node(BaseModel):
    """A unit of work in a Workflow; the walking skeleton supports only shell Nodes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    kind: Literal["shell"]
    inputs: ShellNodeInputs


class Workflow(BaseModel):
    """A normalized Workflow IR for one Run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    version: int
    nodes: tuple[Node, ...]


def normalize_workflow(raw: dict[str, Any], source: str) -> Workflow:
    """Normalize a raw workflow mapping into the Workflow IR, or fail with field paths."""
    try:
        return Workflow.model_validate(raw)
    except ValueError as exc:
        raise WorkflowConfigError(f"invalid workflow definition in {source}: {exc}") from exc
