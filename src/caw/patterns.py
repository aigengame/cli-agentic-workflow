"""Pattern Expanders: compile reusable shapes into plain IR at normalize time.

A **Pattern Expander** compiles a reusable workflow shape into plain ``Workflow``
nodes and edges inside a single Run at normalize time (CONTEXT.md, ADR 0002,
ADR 0008). The authoring surface is a top-level ``pattern:`` block in the workflow
YAML, mutually exclusive with ``nodes:``: a file declares EITHER ``pattern:`` (the
expander materializes the nodes) OR ``nodes:`` (hand-authored), never both.

The registry is the reusable primitive (#13 registers three more expanders): an
expander declares its own pydantic params model and an ``expand`` function that
returns a list of plain node dicts. ``normalize_workflow`` runs ``expand_pattern``
BEFORE ``Workflow.model_validate``, so the product of expansion is an ordinary
``Workflow`` — acyclic validation, ``definition_checksum``, ``caw graph``, the
resume snapshot, and ``execute_run`` all operate on it unchanged. An expanded
workflow is therefore IDENTICAL to its hand-authored ``nodes:`` equivalent.

Registering a new expander is additive: ``register_expander`` adds a registry
entry, with no edit to a dispatch elsewhere.
"""

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from caw.config import WorkflowConfigError

# A node dict is a plain authored-node mapping (id / kind / inputs / needs / ...),
# exactly what a hand-authored `nodes:` entry is — so the expanded list validates
# through the same Workflow IR with no special-casing.
NodeDict = dict[str, Any]

# An expander turns its validated params model into a list of plain node dicts.
ExpandFn = Callable[[Any], list[NodeDict]]


def _reject_declared_needs(node: NodeDict | None, role: str, reason: str) -> NodeDict | None:
    """Reject a node dict that declares its own ``needs`` — the expander owns it.

    Every expander injects the ``needs`` edges that define its shape (the chaining,
    the fan-in, the classifier dependency), so an authored ``needs`` would fight the
    expansion. Sharing this guard keeps the one-line error message uniform across
    expanders (it always contains "must not declare `needs`", which the seam tests
    assert), while ``role`` names which slot is at fault and ``reason`` says why.
    """
    if isinstance(node, dict) and "needs" in node:
        raise ValueError(f"a {role} must not declare `needs`; {reason}")
    return node


def _reject_declared_needs_each(nodes: list[NodeDict], role: str, reason: str) -> list[NodeDict]:
    """Apply :func:`_reject_declared_needs` to each node in a list, returning it."""
    for node in nodes:
        _reject_declared_needs(node, role, reason)
    return nodes


class PatternExpander:
    """One registered expander: its params model, expand function, and one-line shape.

    ``params_model`` validates the ``pattern:`` block's expander-specific fields
    (failures surface through the one-line ``WorkflowConfigError`` contract with a
    field path); ``expand`` compiles the validated params into plain node dicts;
    ``shape`` is the one-line description ``caw patterns list`` shows.
    """

    def __init__(
        self, name: str, params_model: type[BaseModel], expand: ExpandFn, shape: str
    ) -> None:
        self.name = name
        self.params_model = params_model
        self.expand = expand
        self.shape = shape


# THE registry primitive: name -> expander. `register_expander` is the sole way to
# add an entry, so a new pattern (#13) is additive — no dispatch to edit elsewhere.
_EXPANDERS: dict[str, PatternExpander] = {}


def register_expander(
    name: str, params_model: type[BaseModel], expand: ExpandFn, shape: str
) -> None:
    """Register an expander under ``name`` (additive; the registry is the dispatch)."""
    _EXPANDERS[name] = PatternExpander(name, params_model, expand, shape)


def expander_names() -> tuple[str, ...]:
    """The registered pattern names, sorted — drives ``caw patterns list`` (#13-ready)."""
    return tuple(sorted(_EXPANDERS))


def get_expander(name: str) -> PatternExpander | None:
    """The expander registered under ``name``, or ``None`` if unknown."""
    return _EXPANDERS.get(name)


class _PipelineParams(BaseModel):
    """Params of the ``pipeline`` expander: ordered steps chained into a chain.

    Each step is a plain node dict carrying the same fields a hand-authored node
    has (``id``, ``kind``, ``inputs``, and any ``when`` / ``join`` / ``retries`` /
    ``timeout``); the expander only injects each step's ``needs`` (its predecessor),
    so an explicit ``needs`` on a step is rejected — the chaining is the expander's
    job, not the author's.
    """

    model_config = ConfigDict(extra="forbid")

    steps: list[NodeDict] = Field(min_length=1)

    @field_validator("steps")
    @classmethod
    def _steps_must_not_declare_needs(cls, steps: list[NodeDict]) -> list[NodeDict]:
        for step in steps:
            if isinstance(step, dict) and "needs" in step:
                raise ValueError(
                    "a pipeline step must not declare `needs`; the pipeline chains steps"
                )
        return steps


def _expand_pipeline(params: _PipelineParams) -> list[NodeDict]:
    """Chain each step onto its predecessor via ``needs`` into a linear IR."""
    nodes: list[NodeDict] = []
    previous_id: str | None = None
    for step in params.steps:
        node = dict(step)
        if previous_id is not None:
            node["needs"] = [previous_id]
        nodes.append(node)
        previous_id = node.get("id")
    return nodes


class _ParallelParams(BaseModel):
    """Params of the ``parallel`` expander: independent branches + an optional join.

    Each branch is a plain node dict with no ``needs`` (the branches are
    independent and run concurrently); declaring ``needs`` on a branch is rejected.
    ``join`` is an optional plain node dict that fans the branches in — the
    expander injects its ``needs`` (every branch id), so the join may carry its own
    ``join`` policy (``all`` / ``any``) but not its own ``needs``.
    """

    model_config = ConfigDict(extra="forbid")

    branches: list[NodeDict] = Field(min_length=1)
    join: NodeDict | None = None

    @field_validator("branches")
    @classmethod
    def _branches_must_not_declare_needs(cls, branches: list[NodeDict]) -> list[NodeDict]:
        for branch in branches:
            if isinstance(branch, dict) and "needs" in branch:
                raise ValueError(
                    "a parallel branch must not declare `needs`; branches are independent"
                )
        return branches

    @field_validator("join")
    @classmethod
    def _join_must_not_declare_needs(cls, join: NodeDict | None) -> NodeDict | None:
        if isinstance(join, dict) and "needs" in join:
            raise ValueError(
                "a parallel join must not declare `needs`; the join fans in every branch"
            )
        return join


def _expand_parallel(params: _ParallelParams) -> list[NodeDict]:
    """Emit independent branches and (if declared) a join that needs every branch."""
    nodes: list[NodeDict] = [dict(branch) for branch in params.branches]
    if params.join is not None:
        join_node = dict(params.join)
        join_node["needs"] = [branch.get("id") for branch in params.branches]
        nodes.append(join_node)
    return nodes


def _fan_in(branch_nodes: list[NodeDict], join: NodeDict) -> NodeDict:
    """A join node copy that ``needs`` every branch — the shared fan-in injection.

    classify-and-act, generate-and-filter, and fan-out-synthesis all fan a set of
    branch nodes into one downstream node; this owns that single injected ``needs``
    (every branch id) so the three expanders express only their distinct framing.
    """
    join_node = dict(join)
    join_node["needs"] = [branch.get("id") for branch in branch_nodes]
    return join_node


class _ClassifyAndActParams(BaseModel):
    """Params of the ``classify-and-act`` expander: classify, then act on the label.

    A ``classifier`` agent Node runs first and emits a label in its normalized
    output; each ``branches`` entry is a branch the run acts on, gated by its own
    ``when`` Predicate reading the classifier's output (``path`` addresses into
    ``structured_output`` — the sole conditional mechanism, ADR 0007). The optional
    ``join`` fans the branches in; because only the matching branch runs and the
    rest skip, the join typically carries ``join: any`` (ADR 0007) so it runs on the
    one taken branch. The expander injects each branch's ``needs`` (the classifier)
    and the join's ``needs`` (every branch); the classifier, branch, and join entries
    must not declare their own ``needs`` — only the ``when`` gating is the author's.
    """

    model_config = ConfigDict(extra="forbid")

    classifier: NodeDict
    branches: list[NodeDict] = Field(min_length=1)
    join: NodeDict | None = None

    @field_validator("classifier")
    @classmethod
    def _classifier_must_not_declare_needs(cls, classifier: NodeDict) -> NodeDict:
        return _reject_declared_needs(
            classifier, "classify-and-act classifier", "the classifier is the entry node"
        )  # type: ignore[return-value]

    @field_validator("branches")
    @classmethod
    def _branches_must_not_declare_needs(cls, branches: list[NodeDict]) -> list[NodeDict]:
        return _reject_declared_needs_each(
            branches,
            "classify-and-act branch",
            "the expander needs each branch on the classifier",
        )

    @field_validator("join")
    @classmethod
    def _join_must_not_declare_needs(cls, join: NodeDict | None) -> NodeDict | None:
        return _reject_declared_needs(
            join, "classify-and-act join", "the join fans in every branch"
        )


def _expand_classify_and_act(params: _ClassifyAndActParams) -> list[NodeDict]:
    """Emit the classifier, branches each needing it, and an optional fan-in join."""
    classifier = dict(params.classifier)
    classifier_id = classifier.get("id")
    nodes: list[NodeDict] = [classifier]
    branch_nodes: list[NodeDict] = []
    for branch in params.branches:
        node = dict(branch)
        node["needs"] = [classifier_id]
        branch_nodes.append(node)
    nodes.extend(branch_nodes)
    if params.join is not None:
        nodes.append(_fan_in(branch_nodes, params.join))
    return nodes


class _GenerateAndFilterParams(BaseModel):
    """Params of the ``generate-and-filter`` expander: generate N, then keep the good.

    Each ``generators`` entry is an independent candidate generator (no ``needs``, so
    they run concurrently); the ``filter`` Node fans every generator in and emits the
    accepted candidates. The expander injects the filter's ``needs`` (every
    generator); a generator or the filter declaring its own ``needs`` is rejected.
    """

    model_config = ConfigDict(extra="forbid")

    generators: list[NodeDict] = Field(min_length=1)
    filter: NodeDict

    @field_validator("generators")
    @classmethod
    def _generators_must_not_declare_needs(cls, generators: list[NodeDict]) -> list[NodeDict]:
        return _reject_declared_needs_each(
            generators,
            "generate-and-filter generator",
            "generators are independent and run concurrently",
        )

    @field_validator("filter")
    @classmethod
    def _filter_must_not_declare_needs(cls, filter_: NodeDict) -> NodeDict:
        return _reject_declared_needs(
            filter_, "generate-and-filter filter", "the filter fans in every generator"
        )  # type: ignore[return-value]


def _expand_generate_and_filter(params: _GenerateAndFilterParams) -> list[NodeDict]:
    """Emit independent generators and a filter that needs every generator."""
    nodes: list[NodeDict] = [dict(generator) for generator in params.generators]
    nodes.append(_fan_in(nodes, params.filter))
    return nodes


class _FanOutSynthesisParams(BaseModel):
    """Params of the ``fan-out-synthesis`` expander: fan out, then synthesize.

    Each ``workers`` entry is an independent agent Node (no ``needs``, so they run
    concurrently); the ``synthesize`` Node fans every worker in and synthesizes their
    results into one output. The expander injects the synthesize node's ``needs``
    (every worker); a worker or the synthesize node declaring its own ``needs`` is
    rejected. The synthesize node may carry its own ``join`` policy (ADR 0007).
    """

    model_config = ConfigDict(extra="forbid")

    workers: list[NodeDict] = Field(min_length=1)
    synthesize: NodeDict

    @field_validator("workers")
    @classmethod
    def _workers_must_not_declare_needs(cls, workers: list[NodeDict]) -> list[NodeDict]:
        return _reject_declared_needs_each(
            workers,
            "fan-out-synthesis worker",
            "workers are independent and run concurrently",
        )

    @field_validator("synthesize")
    @classmethod
    def _synthesize_must_not_declare_needs(cls, synthesize: NodeDict) -> NodeDict:
        return _reject_declared_needs(
            synthesize, "fan-out-synthesis synthesize", "the synthesize node fans in every worker"
        )  # type: ignore[return-value]


def _expand_fan_out_synthesis(params: _FanOutSynthesisParams) -> list[NodeDict]:
    """Emit independent workers and a synthesize node that needs every worker."""
    nodes: list[NodeDict] = [dict(worker) for worker in params.workers]
    nodes.append(_fan_in(nodes, params.synthesize))
    return nodes


def expand_pattern(raw: dict[str, Any], source: str) -> dict[str, Any]:
    """Expand a ``pattern:`` block into a plain ``nodes:`` workflow, or pass through.

    Enforces the ``pattern:``-XOR-``nodes:`` authoring surface, looks the expander
    up by ``pattern.type``, validates its params (surfacing failures through the
    one-line ``WorkflowConfigError`` contract with a field path), and returns a new
    raw mapping with the expander's plain node dicts under ``nodes:`` and no
    ``pattern:`` key — so the caller validates an ordinary ``Workflow``. A raw with
    no ``pattern:`` is returned unchanged.
    """
    if "pattern" not in raw:
        return raw
    if "nodes" in raw:
        raise WorkflowConfigError(
            f"invalid workflow definition in {source}: a workflow declares either "
            f"`pattern` or `nodes`, not both"
        )
    pattern = raw["pattern"]
    if not isinstance(pattern, dict):
        raise WorkflowConfigError(
            f"invalid workflow definition in {source}: `pattern` must be a mapping"
        )
    if "type" not in pattern:
        known = ", ".join(expander_names()) or "<none>"
        raise WorkflowConfigError(
            f"invalid workflow definition in {source}: `pattern` must declare a `type` "
            f"(known: {known})"
        )
    pattern_type = pattern["type"]
    expander = get_expander(pattern_type) if isinstance(pattern_type, str) else None
    if expander is None:
        known = ", ".join(expander_names()) or "<none>"
        raise WorkflowConfigError(
            f"invalid workflow definition in {source}: unknown pattern type "
            f"{pattern_type!r} (known: {known})"
        )
    expander_params = {key: value for key, value in pattern.items() if key != "type"}
    try:
        params = expander.params_model.model_validate(expander_params)
    except ValidationError as exc:
        first = exc.errors()[0]
        location = ".".join(str(part) for part in first["loc"]) or "pattern"
        remainder = exc.error_count() - 1
        suffix = f" (+{remainder} more)" if remainder else ""
        raise WorkflowConfigError(
            f"invalid workflow definition in {source}: pattern.{location}: {first['msg']}{suffix}"
        ) from exc
    expanded = {key: value for key, value in raw.items() if key != "pattern"}
    expanded["nodes"] = expander.expand(params)
    return expanded


register_expander(
    "pipeline", _PipelineParams, _expand_pipeline, "ordered steps chained into a linear chain"
)
register_expander(
    "parallel",
    _ParallelParams,
    _expand_parallel,
    "independent branches run concurrently, optionally joined downstream",
)
register_expander(
    "classify-and-act",
    _ClassifyAndActParams,
    _expand_classify_and_act,
    "a classifier gates `when`-conditioned branches, optionally joined downstream",
)
register_expander(
    "generate-and-filter",
    _GenerateAndFilterParams,
    _expand_generate_and_filter,
    "parallel generators feed a filter that emits the accepted candidates",
)
register_expander(
    "fan-out-synthesis",
    _FanOutSynthesisParams,
    _expand_fan_out_synthesis,
    "parallel workers fan out, a synthesize node joins their results",
)
