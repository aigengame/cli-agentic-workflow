"""The minimal Workflow IR: typed and immutable once a Run starts (ADR 0002)."""

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

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
    needs: tuple[str, ...] = ()

    _id_non_blank = field_validator("id")(_require_non_blank)

    @model_validator(mode="after")
    def _must_not_need_itself(self) -> "Node":
        if self.id in self.needs:
            raise ValueError(f"node {self.id!r} must not need itself")
        return self


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

    @field_validator("nodes")
    @classmethod
    def _needs_must_reference_known_nodes(cls, nodes: tuple[Node, ...]) -> tuple[Node, ...]:
        known = {node.id for node in nodes}
        for node in nodes:
            for reference in node.needs:
                if reference not in known:
                    raise ValueError(f"node {node.id!r} needs unknown node {reference!r}")
        return nodes

    @field_validator("nodes")
    @classmethod
    def _needs_must_be_acyclic(cls, nodes: tuple[Node, ...]) -> tuple[Node, ...]:
        # Kahn's algorithm: peel off nodes whose needs are all satisfied; whatever
        # remains lies on or downstream of a dependency cycle. This runs after the
        # known-reference validator, so every need of a remaining node is itself
        # either peeled or remaining.
        done: set[str] = set()
        remaining = list(nodes)
        progressed = True
        while progressed:
            progressed = False
            for node in list(remaining):
                if done.issuperset(node.needs):
                    done.add(node.id)
                    remaining.remove(node)
                    progressed = True
        if remaining:
            cycle = " -> ".join(_find_cycle(remaining))
            raise ValueError(f"dependency cycle: {cycle}")
        return nodes


def _find_cycle(remaining: list[Node]) -> list[str]:
    """Extract one actual cycle from the unpeelable remainder of Kahn's algorithm.

    Walks `needs` references between remaining nodes until one repeats; the
    rendered path reads left to right along `needs` (x -> y means x needs y).
    """
    by_id = {node.id: node for node in remaining}
    path: list[str] = []
    first_seen_at: dict[str, int] = {}
    current = remaining[0]
    while current.id not in first_seen_at:
        first_seen_at[current.id] = len(path)
        path.append(current.id)
        current = next(by_id[reference] for reference in current.needs if reference in by_id)
    return [*path[first_seen_at[current.id] :], current.id]


def execution_order(workflow: Workflow) -> tuple[Node, ...]:
    """The deterministic topological order a Run executes Nodes in.

    Among Nodes whose dependencies are all satisfied, declaration order in the
    workflow definition breaks the tie.
    """
    ordered: list[Node] = []
    done: set[str] = set()
    remaining = list(workflow.nodes)
    while remaining:
        node = next(n for n in remaining if done.issuperset(n.needs))
        ordered.append(node)
        done.add(node.id)
        remaining.remove(node)
    return tuple(ordered)


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
