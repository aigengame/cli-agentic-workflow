"""The minimal Workflow IR: typed and immutable once a Run starts (ADR 0002)."""

import functools
import hashlib
import heapq
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from caw.config import WorkflowConfigError


def _require_non_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be blank or whitespace-only")
    return value


class ShellNodeInputs(BaseModel):
    """Inputs of a shell Node."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["shell"] = "shell"
    command: str

    _command_non_blank = field_validator("command")(_require_non_blank)


class AgentNodeInputs(BaseModel):
    """Inputs of an agent Node: how it selects an Adapter and what it sends.

    ``adapter`` names the Adapter that invokes the external Agent CLI (the mock
    Adapter in v0.1). ``output_schema`` and ``fixture`` are file paths the kernel
    and the mock Adapter resolve relative to their own working directory; they
    are validated for shape here and for existence at execution time, so an
    authoring typo still fails as a node error rather than a crash. ``env`` is a
    declaration of variable NAMES, never values: only the named variables reach
    the node process, and the env policy keeps their values out of State, Events,
    and Artifacts (#5).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["agent"] = "agent"
    adapter: str
    prompt: str
    args: tuple[str, ...] = ()
    env: tuple[str, ...] = ()
    output_schema: Path | None = None
    fixture: Path | None = None

    _adapter_non_blank = field_validator("adapter")(_require_non_blank)
    _prompt_non_blank = field_validator("prompt")(_require_non_blank)

    @field_validator("env")
    @classmethod
    def _env_names_must_be_unique_and_non_blank(cls, names: tuple[str, ...]) -> tuple[str, ...]:
        seen: set[str] = set()
        for name in names:
            if not name.strip():
                raise ValueError("env names must not be blank or whitespace-only")
            if name in seen:
                raise ValueError(f"duplicate env name {name!r}")
            seen.add(name)
        return names


NodeInputs = Annotated[
    ShellNodeInputs | AgentNodeInputs,
    Field(discriminator="kind"),
]


class Node(BaseModel):
    """A unit of work in a Workflow; v0.1 supports shell and agent Nodes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    kind: Literal["shell", "agent"]
    inputs: NodeInputs
    needs: tuple[str, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def _stamp_inputs_kind(cls, data: object) -> object:
        # `kind` lives at the node level in workflow definitions, never inside
        # `inputs`. Copy it down so the discriminated `inputs` union resolves to
        # the matching inputs model; a mismatching explicit `inputs.kind` is left
        # to surface as a discriminator error rather than being silently rewritten.
        if isinstance(data, dict):
            kind = data.get("kind")
            inputs = data.get("inputs")
            if isinstance(kind, str) and isinstance(inputs, dict) and "kind" not in inputs:
                inputs = {**inputs, "kind": kind}
                data = {**data, "inputs": inputs}
        return data

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
    # The maximum number of node Attempts the executor runs concurrently. The
    # default leans conservative (4): enough to run a typical fan-out — including
    # the canonical three-branch parallel — without serializing it, while
    # staying well below OS subprocess and file-descriptor pressure (ADR 0003).
    concurrency: int = Field(default=4, ge=1)

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
            # Quote each id (!r) so control characters cannot break the
            # one-error-line contract.
            cycle = " -> ".join(repr(node_id) for node_id in _find_cycle(remaining))
            raise ValueError(f"dependency cycle: {cycle}")
        return nodes


@functools.lru_cache(maxsize=8)
def _peel_in_declaration_order(
    nodes: tuple[Node, ...],
) -> tuple[tuple[Node, ...], tuple[Node, ...]]:
    """Kahn's algorithm over needs edges, preferring declaration order among ready nodes.

    Returns the peeled (topologically ordered) nodes and the unpeelable remainder;
    a non-empty remainder lies on or downstream of a dependency cycle.

    Classic in-degree Kahn at O((V + E) log V): adjacency and in-degree counts
    are built once, and ready nodes are kept in a heap keyed by declaration
    index so the deterministic declaration-order tie-break among ready nodes is
    preserved exactly. The result is memoized on the frozen `nodes` tuple so the
    acyclicity validator and `execution_order` share one peel per invocation
    instead of recomputing it (issue #43).
    """
    index_of = {node.id: index for index, node in enumerate(nodes)}
    dependents: dict[str, list[Node]] = {node.id: [] for node in nodes}
    indegree: dict[str, int] = {}
    for node in nodes:
        # Count only needs that reference a known node; the known-reference
        # validator runs before this, so for a normally constructed Workflow
        # every need is present. A validation-bypassed Workflow with a dangling
        # need simply leaves the dependent unpeelable, surfacing in the
        # remainder rather than crashing here.
        present = [reference for reference in node.needs if reference in index_of]
        indegree[node.id] = len(present)
        for reference in present:
            dependents[reference].append(node)

    ready: list[tuple[int, str]] = [
        (index_of[node.id], node.id) for node in nodes if indegree[node.id] == 0
    ]
    heapq.heapify(ready)
    by_id = {node.id: node for node in nodes}

    ordered: list[Node] = []
    while ready:
        _, node_id = heapq.heappop(ready)
        ordered.append(by_id[node_id])
        for dependent in dependents[node_id]:
            indegree[dependent.id] -= 1
            if indegree[dependent.id] == 0:
                heapq.heappush(ready, (index_of[dependent.id], dependent.id))

    peeled = set(node.id for node in ordered)
    remaining = tuple(node for node in nodes if node.id not in peeled)
    return tuple(ordered), remaining


def _find_cycle(remaining: Sequence[Node]) -> list[str]:
    """Extract one actual cycle from the unpeelable remainder of Kahn's algorithm.

    Walks `needs` references between remaining nodes until one repeats. The
    returned path reads left to right in execution direction (x -> y means y
    needs x, so x would run before y), matching the JSON plan's edge direction.
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
    needs_walk = [*path[first_seen_at[current.id] :], current.id]
    return needs_walk[::-1]


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
    return ordered


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


_INPUTS_DISCRIMINATOR_TAGS = frozenset({"shell", "agent"})


def _strip_inputs_discriminator(loc: tuple[int | str, ...]) -> tuple[int | str, ...]:
    """Drop the ``inputs`` discriminator tag pydantic injects into the error path.

    A discriminated-union field error carries the chosen tag as a path segment,
    e.g. ``(..., 'inputs', 'shell', 'command')``. That tag is an internal routing
    detail, not a field a workflow author wrote, so it would make the one-error
    line read ``inputs.shell.command``. Drop it so the path names the authored
    field (``inputs.command``), preserving the error-location contract.
    """
    for index, part in enumerate(loc):
        if (
            part == "inputs"
            and index + 1 < len(loc)
            and loc[index + 1] in _INPUTS_DISCRIMINATOR_TAGS
        ):
            return (*loc[: index + 1], *loc[index + 2 :])
    return loc


def _render_location(loc: tuple[int | str, ...], raw: dict[str, Any]) -> str:
    loc = _strip_inputs_discriminator(loc)
    parts = [str(part) for part in loc]
    if len(loc) >= 2 and loc[0] == "nodes" and isinstance(loc[1], int):
        # Pair the position with the quoted id: nodes[1 'greet'].kind stays
        # unambiguous for integer-like ids and for duplicate ids, where the
        # index alone or the id alone would mislead.
        node_id = _node_id_at(raw, loc[1])
        if node_id is not None:
            parts[:2] = [f"nodes[{loc[1]} {node_id!r}]"]
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
