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

    @field_validator("needs", mode="before")
    @classmethod
    def _needs_must_be_a_list_of_unique_ids(cls, value: object) -> object:
        if isinstance(value, str) or not isinstance(value, list | tuple):
            raise ValueError("must be a list of node ids")
        seen: set[str] = set()
        for entry in value:
            if isinstance(entry, str):
                if entry in seen:
                    raise ValueError(f"duplicate needs entry {entry!r}")
                seen.add(entry)
        return value

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
        # This runs after the known-reference validator, so every need of a
        # remaining node is itself either peeled or remaining.
        _, remaining = _peel_in_declaration_order(nodes)
        if remaining:
            cycle = " -> ".join(_find_cycle(remaining))
            raise ValueError(f"dependency cycle: {cycle}")
        return nodes


def _peel_in_declaration_order(nodes: tuple[Node, ...]) -> tuple[list[Node], list[Node]]:
    """Kahn's algorithm over needs edges, preferring declaration order among ready nodes.

    Returns the peeled (topologically ordered) nodes and the unpeelable remainder;
    a non-empty remainder lies on or downstream of a dependency cycle.
    """
    ordered: list[Node] = []
    done: set[str] = set()
    remaining = list(nodes)
    while remaining:
        node = next((n for n in remaining if done.issuperset(n.needs)), None)
        if node is None:
            break
        ordered.append(node)
        done.add(node.id)
        remaining.remove(node)
    return ordered, remaining


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
        successor = next(
            (by_id[reference] for reference in current.needs if reference in by_id), None
        )
        if successor is None:
            # Raise ValueError, never StopIteration: pydantic converts only
            # ValueError/AssertionError into validation errors, so a breach of
            # the known-reference invariant must not escape as a raw traceback.
            raise ValueError(
                f"node {current.id!r} is unorderable but lies on no cycle; "
                f"it needs a node outside the workflow"
            )
        current = successor
    return [*path[first_seen_at[current.id] :], current.id]


def execution_order(workflow: Workflow) -> tuple[Node, ...]:
    """The deterministic topological order a Run executes Nodes in.

    Among Nodes whose dependencies are all satisfied, declaration order in the
    workflow definition breaks the tie. A normally constructed Workflow is
    validated acyclic, so every Node is ordered; a Workflow that bypassed
    validation (model_construct, model_copy(update=...)) and violates that
    invariant fails loudly instead of yielding a partial order.
    """
    ordered, remaining = _peel_in_declaration_order(workflow.nodes)
    if remaining:
        unorderable = ", ".join(repr(node.id) for node in remaining)
        raise ValueError(
            f"workflow {workflow.name!r} has unorderable nodes (dependency cycle "
            f"or unknown reference; was validation bypassed?): {unorderable}"
        )
    return tuple(ordered)


def _node_id_at(raw: dict[str, Any], index: int) -> str | None:
    nodes = raw.get("nodes")
    if not isinstance(nodes, list) or not 0 <= index < len(nodes):
        return None
    entry = nodes[index]
    if not isinstance(entry, dict):
        return None
    node_id = entry.get("id")
    if isinstance(node_id, str) and node_id.strip():
        return node_id
    return None


def _render_location(loc: tuple[int | str, ...], raw: dict[str, Any]) -> str:
    parts = [str(part) for part in loc]
    if len(loc) >= 2 and loc[0] == "nodes" and isinstance(loc[1], int):
        # Name the node by id where possible: nodes[greet].kind beats nodes.0.kind.
        node_id = _node_id_at(raw, loc[1])
        if node_id is not None:
            parts[:2] = [f"nodes[{node_id}]"]
    return ".".join(parts) or "workflow"


def _first_error_line(exc: ValidationError, raw: dict[str, Any]) -> str:
    first = exc.errors()[0]
    location = _render_location(first["loc"], raw)
    remainder = exc.error_count() - 1
    suffix = f" (+{remainder} more)" if remainder else ""
    return f"{location}: {first['msg']}{suffix}"


def normalize_workflow(raw: dict[str, Any], source: str) -> Workflow:
    """Normalize a raw workflow mapping into the Workflow IR, or fail with field paths."""
    try:
        return Workflow.model_validate(raw)
    except ValidationError as exc:
        raise WorkflowConfigError(
            f"invalid workflow definition in {source}: {_first_error_line(exc, raw)}"
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
