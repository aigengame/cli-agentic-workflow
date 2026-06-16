"""The minimal Workflow IR: typed and immutable once a Run starts (ADR 0002)."""

import functools
import hashlib
import heapq
import json
import re
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Literal, TypeVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from caw.adapter import BUILTIN_ADAPTER_NAMES
from caw.config import WorkflowConfigError
from caw.patterns import expand_pattern

# The result type a `Predicate.fold` reduces a predicate tree to: a bool for the
# evaluator, a string for the CLI summary, a dict for the plan serializer (#77).
_T = TypeVar("_T")


def _require_non_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be blank or whitespace-only")
    return value


# A declared env entry must be a valid POSIX environment-variable NAME: a leading
# letter or underscore followed by letters, digits, or underscores. This rejects
# value-shaped entries (``API_TOKEN=s3cr3t``), any embedded ``=``, leading digits,
# spaces, and the empty string — so a secret value can never be smuggled into the
# allow-list and persisted into the normalized snapshot (#66).
_ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _unique_valid_env_names(
    names: tuple[str, ...] | None,
) -> tuple[str, ...] | None:
    """Validate a node's declared env allow-list: unique, valid env NAMES (#5, #66).

    Shared by both node kinds so a shell Node's ``env`` has the same allow-list
    semantics as an agent Node's: it declares variable NAMES, never values. Each
    entry must be a valid POSIX environment-variable name (``^[A-Za-z_][A-Za-z0-9_]*$``)
    — this rejects a ``NAME=value`` form, any embedded ``=``, a leading digit, a
    space, and the empty/blank string — and a duplicate name is a config error.

    ``None`` is the OMITTED-``env`` sentinel (the field default), distinct from an
    explicit empty ``()``: it carries no names to validate and passes through so the
    executor can preserve legacy parent-environment inheritance for an undeclared
    Node while an explicit empty allow-list passes no variables (#66, ADR 0006).
    """
    if names is None:
        return None
    seen: set[str] = set()
    for name in names:
        if not _ENV_NAME_PATTERN.match(name):
            raise ValueError(
                f"env entry {name!r} is not a valid env variable name "
                f"(names only, never values; must match {_ENV_NAME_PATTERN.pattern})"
            )
        if name in seen:
            raise ValueError(f"duplicate env name {name!r}")
        seen.add(name)
    return names


class ShellNodeInputs(BaseModel):
    """Inputs of a shell Node.

    ``env`` is a node-generic declaration of variable NAMES, never values: only the
    named variables reach the shell process, giving a shell Node the same env
    allow-list as an agent Node, and the env policy keeps their values out of
    State, Events, and the snapshot (#5, #66).

    The default ``None`` is the OMITTED ``env``, distinct from an explicit empty
    ``[]``: an omitted ``env`` lets the shell inherit the parent environment
    (legacy behavior), while an explicit empty allow-list declares that NO variable
    crosses the seam (ADR 0006). The two serialize distinctly in the snapshot
    (absent vs ``[]``) so a resume reconstructs the SAME env scope (#66).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["shell"] = "shell"
    command: str
    env: tuple[str, ...] | None = None

    _command_non_blank = field_validator("command")(_require_non_blank)
    _env_names_valid = field_validator("env")(_unique_valid_env_names)


class AgentNodeInputs(BaseModel):
    """Inputs of an agent Node: how it selects an Adapter and what it sends.

    ``adapter`` names the Adapter that invokes the external Agent CLI (the mock
    Adapter in v0.1); an unknown built-in adapter name fails validation fast (#64).
    ``output_schema`` and ``fixture`` are file paths: a relative path is anchored
    to the workflow file's directory at normalize time (not the process CWD), so
    the same definition runs identically from any working directory (#64); an
    absolute path is used as-is. Existence is checked at execution time, so an
    authoring typo fails as a node error rather than a crash. ``env`` is a
    declaration of variable NAMES, never values: only the named variables reach
    the node process, and the env policy keeps their values out of State, Events,
    and Artifacts (#5). Its default ``None`` is the OMITTED ``env``, distinct from
    an explicit empty ``[]`` (an empty allow-list), and the two serialize distinctly
    in the snapshot so a resume reconstructs the SAME env scope (#66).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["agent"] = "agent"
    adapter: str
    prompt: str
    args: tuple[str, ...] = ()
    env: tuple[str, ...] | None = None
    output_schema: Path | None = None
    fixture: Path | None = None

    _adapter_non_blank = field_validator("adapter")(_require_non_blank)
    _prompt_non_blank = field_validator("prompt")(_require_non_blank)
    _env_names_valid = field_validator("env")(_unique_valid_env_names)

    @field_validator("adapter")
    @classmethod
    def _adapter_must_be_known(cls, adapter: str, info: ValidationInfo) -> str:
        # A typo'd / unknown adapter name fails validation fast (#64), before any
        # run directory. The known set is the built-in adapter names by default;
        # a caller injecting adapters at run time passes its known names through
        # the validation context, since those names are not knowable from the
        # definition alone.
        known = BUILTIN_ADAPTER_NAMES
        if isinstance(info.context, dict):
            override = info.context.get("known_adapters")
            if override is not None:
                known = frozenset(override)
        if adapter not in known:
            allowed = ", ".join(sorted(known)) or "<none>"
            raise ValueError(f"unknown adapter {adapter!r} (known: {allowed})")
        return adapter


# The node-level `kind` is the single source of truth (#62): it selects which
# inputs model is built, so the top-level kind, the `caw graph` plan, and the
# executor dispatch can never disagree. Because the concrete model is chosen by
# kind (not by a discriminated union), validation errors name the authored field
# directly (`inputs.command`) with no injected discriminator tag to strip.
_INPUTS_MODEL_FOR_KIND: dict[str, type[ShellNodeInputs | AgentNodeInputs]] = {
    "shell": ShellNodeInputs,
    "agent": AgentNodeInputs,
}


_RELATIVE_PATH_INPUTS = ("output_schema", "fixture")


def _resolve_inputs_paths(inputs: dict[str, Any], context: Any) -> dict[str, Any]:
    """Anchor relative agent-Node file paths to the workflow file's directory (#64).

    ``output_schema`` and ``fixture`` are file paths; resolving a relative one
    against the workflow file's directory (the ``base_dir`` in the validation
    context) rather than the kernel process CWD makes the same definition run
    identically from any working directory. An absolute path is left untouched,
    and with no ``base_dir`` the paths pass through as authored.
    """
    if not isinstance(context, dict):
        return inputs
    base_dir = context.get("base_dir")
    if base_dir is None or inputs.get("kind") != "agent":
        return inputs
    resolved = dict(inputs)
    for key in _RELATIVE_PATH_INPUTS:
        value = resolved.get(key)
        if isinstance(value, str | Path):
            path = Path(value)
            if not path.is_absolute():
                resolved[key] = str(Path(base_dir) / path)
    return resolved


def _build_inputs(
    model: type[ShellNodeInputs | AgentNodeInputs],
    inputs: dict[str, Any],
    context: Any,
) -> ShellNodeInputs | AgentNodeInputs:
    """Build the kind's inputs model, re-raising any failure under the `inputs` field.

    Validating the inputs model here (rather than via a discriminated union on the
    field) means an inner failure carries the inputs-local path (``command``);
    re-prefixing it with ``inputs`` keeps the rendered location precise
    (``inputs.command``) without the discriminated union's injected tag — so #62
    removes the discriminator-strip workaround entirely. The validation context
    flows down so the adapter-known check (#64) sees any caller-injected adapters.
    """
    try:
        return model.model_validate(inputs, context=context)
    except ValidationError as exc:
        raise ValidationError.from_exception_data(
            exc.title, [_reprefix_inputs(error) for error in exc.errors()]
        ) from exc


def _reprefix_inputs(error: Any) -> Any:
    """Re-locate one inner-inputs error under the node's ``inputs`` field.

    Preserves the original error type so the rendered message is unchanged (a
    ``missing`` stays "Field required", a ``value_error`` carries its own
    message), only prepending ``inputs`` to the location path.
    """
    relocated: dict[str, Any] = {
        "type": error["type"],
        "loc": ("inputs", *error["loc"]),
        "input": error.get("input"),
    }
    ctx = error.get("ctx")
    if ctx is not None:
        # A value_error renders from ctx['error']; keep the original ValueError so
        # the message is not double-prefixed with "Value error,".
        relocated["ctx"] = ctx
    return relocated


# The atomic data sources a predicate leaf may read off an upstream Node's
# normalized output (#7). They mirror NodeResult.normalized_output's keys, so a
# `when` reads exactly what State persists: the textual `stdout`, the integer
# `exit_status`, or the agent Node's parsed `structured_output`.
PredicateField = Literal["stdout", "exit_status", "structured_output"]

# The comparison operators v0.1 implements (#7). The shape admits more later
# (not_equals, gt, lt, matches, in) with no restructuring; only this Literal
# changes. `contains` is a substring test and is valid only on a string field.
PredicateOp = Literal["equals", "contains"]

# The only field whose value is guaranteed a string, so the only field `contains`
# (a substring test) is valid against (#7). `exit_status` is an integer and
# `structured_output` is arbitrary parsed JSON, so `contains` on either is a
# config error rather than a meaningless run-time comparison.
_STRING_PREDICATE_FIELDS = frozenset({"stdout"})


# The normalized-output fields each Node KIND can produce, so a `when` ref to a
# field a dependency's kind never emits is a config error (#75). Every Node emits
# `stdout` and `exit_status`; only an agent Node emits `structured_output` (a
# shell Node has no parsed structured output). The kind-aware Workflow validator
# checks each leaf ref against the referenced node's kind.
_PRODUCIBLE_FIELDS_FOR_KIND: dict[str, frozenset[str]] = {
    "shell": frozenset({"stdout", "exit_status"}),
    "agent": frozenset({"stdout", "exit_status", "structured_output"}),
}


# The Python types a leaf `value` may take per referenced field, so a `value`
# whose type can NEVER match the field is rejected at config time rather than
# silently evaluating false on every run (#75). `exit_status` is an integer, so
# only an `int` value can match — and a `bool` is EXCLUDED even though Python
# treats `True == 1` / `False == 0`, because that aliasing is exactly the
# confusing always-or-never match the leaf evaluator already refuses (#74); a
# bool against an int field is an authoring mistake, not an intended comparison.
# `stdout` is a string, so only a `str` value can match. `structured_output` is
# arbitrary parsed JSON addressed by an optional sub-`path`, so any scalar leaf
# value (`str` / `int` / `bool`) may legitimately compare against the addressed
# sub-value; its type is not constrained here.
#
# Note that `bool` is a subclass of `int` in Python, so an `exit_status` check
# must test bool BEFORE int (a bare ``isinstance(value, int)`` would admit a
# bool); the validator orders its checks accordingly.
def _value_type_admissible_for_field(field: str, value: object) -> bool:
    if field == "exit_status":
        return isinstance(value, int) and not isinstance(value, bool)
    if field == "stdout":
        return isinstance(value, str)
    # structured_output: any scalar value may match the addressed sub-value.
    return True


class PredicateRef(BaseModel):
    """An atomic reference to one field of an upstream Node's normalized output (#7).

    ``path`` is an optional sub-path ADDRESSING into the ``structured_output``
    field (#75): an ordered sequence of dict keys (``str``) and/or list indices
    (``int``) that descends into the parsed JSON to the scalar a leaf compares
    against, e.g. ``path: ["category"]`` addresses ``structured_output["category"]``
    and ``path: ["items", 0]`` addresses the first element of an ``items`` list.
    Without a ``path`` a ``structured_output`` ref reads the whole parsed object
    (a scalar ``equals`` can then never match a dict, so addressing is how a
    classify-and-act routes on a classifier's label — #13). A ``path`` on any
    other field (``stdout`` / ``exit_status``, scalars with no interior) is a
    config error: there is nothing to descend into. This is the single, reusable
    addressing mechanism #13 builds classify-and-act routing on.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    node: str
    field: PredicateField
    path: tuple[str | int, ...] = ()

    _node_non_blank = field_validator("node")(_require_non_blank)

    @field_validator("path", mode="before")
    @classmethod
    def _path_steps_are_keys_or_indices(cls, value: object) -> object:
        # A `path` step is a dict key (str) or a list index (int) — never a bool.
        # `tuple[str | int, ...]` would otherwise coerce a `bool` to an int (Python
        # makes `True`/`False` ints), silently addressing list element 1/0 the
        # author never named; reject it BEFORE coercion so `path: [true]` is an
        # actionable config error, not a silent index (#75 review NIT).
        if isinstance(value, list | tuple):
            for step in value:
                if isinstance(step, bool):
                    raise ValueError(
                        f"`path` step {step!r} is a bool; a step is a dict key (str) "
                        f"or a list index (int), never a bool"
                    )
        return value

    @model_validator(mode="after")
    def _path_only_addresses_structured_output(self) -> "PredicateRef":
        # A sub-`path` descends into the structured_output object; `stdout` and
        # `exit_status` are scalars with no interior to address, so a `path` on
        # either is a config error rather than a silently-ignored attribute (#75).
        if self.path and self.field != "structured_output":
            raise ValueError(
                f"`path` addresses into `structured_output` only, not the scalar "
                f"{self.field!r} field"
            )
        return self


class Predicate(BaseModel):
    """A node-level `when` predicate: a recursive, composable boolean algebra (#7).

    A predicate is EXACTLY ONE shape — a leaf XOR one combinator:

    - leaf: a ``ref`` to one upstream field, an ``op``, and a ``value`` (one
      reference -> comparison, the indivisible atom);
    - ``all_of`` / ``any_of``: a list of sub-predicates combined by AND / OR;
    - ``not``: a single negated sub-predicate (the Python attribute is ``not_``
      since ``not`` is a keyword; the authored field stays ``not``).

    Combinators nest arbitrarily, so the algebra is composable and extensible.
    CONTEXT.md makes ``when`` the sole conditional mechanism — Edges carry no
    conditions — so the predicate is modelled structurally (no string eval, no
    parser surface).
    """

    # `not` is reserved in Python, so the attribute is `not_` while the authored
    # and SERIALIZED field stays `not`. ``serialize_by_alias`` makes the generic
    # ``model_dump(mode="json")`` (used by the snapshot and definition_checksum)
    # emit ``not`` with no per-call ``by_alias`` flag, and ``populate_by_name``
    # lets a re-validation of that dump (resume reconstructs the Workflow with
    # ``Workflow.model_validate``) accept either spelling — so the predicate
    # round-trips through the snapshot losslessly.
    model_config = ConfigDict(
        frozen=True, extra="forbid", populate_by_name=True, serialize_by_alias=True
    )

    # Leaf fields.
    ref: PredicateRef | None = None
    op: PredicateOp | None = None
    value: str | int | bool | None = None
    # Combinator fields. `not` is reserved in Python, so the attribute is `not_`
    # while the authored/serialized field stays `not` (alias).
    all_of: tuple["Predicate", ...] | None = None
    any_of: tuple["Predicate", ...] | None = None
    not_: "Predicate | None" = Field(default=None, alias="not")

    def _present_shapes(self) -> list[str]:
        """The shape kinds this predicate declares, by field presence.

        The SINGLE place that maps a Predicate's fields to its shape (#77): the
        shape validator counts these to enforce "exactly one shape", and ``fold``
        dispatches on the single present shape. Adding a combinator extends this
        one list, so every shape-dispatching consumer follows automatically rather
        than each re-deriving the mapping.
        """
        return [
            name
            for name, present in (
                ("leaf", self.ref is not None),
                ("all_of", self.all_of is not None),
                ("any_of", self.any_of is not None),
                ("not", self.not_ is not None),
            )
            if present
        ]

    def fold(
        self,
        *,
        leaf: "Callable[[Predicate], _T]",
        all_of: "Callable[[list[_T]], _T]",
        any_of: "Callable[[list[_T]], _T]",
        not_: "Callable[[_T], _T]",
    ) -> "_T":
        """Recursively reduce this predicate to a ``_T`` via per-shape callbacks (#77).

        The single shape-dispatch site every consumer drives: the evaluator, the
        leaf-ref walk, the CLI summary, and the plan serializer all express their
        recursion as a fold rather than re-implementing the ``leaf`` / ``all_of``
        / ``any_of`` / ``not`` dispatch. ``leaf`` receives the leaf Predicate (to
        read its ``ref`` / ``op`` / ``value``); each combinator callback receives
        its children ALREADY folded, so a consumer only describes how to combine
        results, never how to walk the tree. Adding a combinator adds one shape to
        ``_present_shapes`` and one branch here — and every consumer follows.

        Assumes a validated single-shape predicate (the model validator guarantees
        it); the final ``not`` branch is the residual shape.
        """

        def recurse(child: "Predicate") -> "_T":
            return child.fold(leaf=leaf, all_of=all_of, any_of=any_of, not_=not_)

        if self.ref is not None:
            return leaf(self)
        if self.all_of is not None:
            return all_of([recurse(child) for child in self.all_of])
        if self.any_of is not None:
            return any_of([recurse(child) for child in self.any_of])
        assert self.not_ is not None, "a validated non-leaf predicate is exactly one combinator"
        return not_(recurse(self.not_))

    def to_plan_dict(self) -> dict[str, Any]:
        """Serialize this predicate to its plan/JSON form: the ACTIVE shape only (#77).

        Unlike ``model_dump(exclude_none=True)`` — which strips an intentional
        falsy/None leaf field as if it were an inactive-shape key — this folds the
        validated shape and emits exactly the keys that shape carries: a leaf emits
        ``ref`` (its ``path`` only when non-empty), ``op``, and ``value`` (a value
        is always present and meaningful — `value: null` is rejected at validation,
        so a serialized ``value`` is never a stripped key); a combinator emits its
        single combinator key with its children serialized. So the plan round-trips
        the shape with no inactive None keys and no spurious empty ``path``.
        """

        def _leaf(node: "Predicate") -> dict[str, Any]:
            assert node.ref is not None
            ref: dict[str, Any] = {"node": node.ref.node, "field": node.ref.field}
            if node.ref.path:
                ref["path"] = list(node.ref.path)
            return {"ref": ref, "op": node.op, "value": node.value}

        return self.fold(
            leaf=_leaf,
            all_of=lambda children: {"all_of": children},
            any_of=lambda children: {"any_of": children},
            not_=lambda child: {"not": child},
        )

    @model_validator(mode="after")
    def _exactly_one_shape(self) -> "Predicate":
        # A predicate is EXACTLY one shape — a leaf XOR one combinator (#7). A
        # leaf is identified by `ref` and must be complete (`ref` AND `op`); each
        # combinator is identified by its own field. Mixing shapes, declaring two
        # combinators, or an empty/incomplete predicate is a config error, so a
        # `when` always denotes one unambiguous boolean expression. The shape
        # mapping lives once in `_present_shapes` (#77); this counts it.
        present = self._present_shapes()
        is_leaf = "leaf" in present
        shapes = len(present)
        if shapes != 1:
            raise ValueError(
                "must be exactly one of a leaf (ref/op/value), all_of, any_of, or not"
            )
        if is_leaf and self.op is None:
            raise ValueError("a leaf predicate must declare both `ref` and `op`")
        if not is_leaf and (self.op is not None or self.value is not None):
            raise ValueError("`op`/`value` belong to a leaf predicate, not a combinator")
        # A leaf must carry a `value` to compare against (#75): `value` is REQUIRED.
        # A forgotten `value` defaults to None and would silently become a
        # near-always-false gate (with `contains` it degrades to `'None' in actual`),
        # so a missing value is a config error. `equals null` is not a supported
        # comparison in v0.1: no normalized field is ever JSON null (exit_status is
        # int, stdout is str; a missing structured_output sub-path is ABSENCE, which
        # evaluates false, not a null match), so there is nothing a null value could
        # meaningfully match.
        if is_leaf and self.value is None:
            raise ValueError(
                "a leaf predicate must declare a `value` to compare against "
                "(`value: null` is not a supported comparison)"
            )
        # A leaf `value` whose TYPE can never match its `field` is rejected here, so
        # a type-mismatched gate (a string or bool against the integer `exit_status`)
        # fails at config time rather than silently evaluating false on every run
        # (#75). `structured_output` is type-unconstrained (any scalar may match the
        # addressed sub-value).
        if (
            is_leaf
            and self.ref is not None
            and self.value is not None
            and not _value_type_admissible_for_field(self.ref.field, self.value)
        ):
            raise ValueError(
                f"`value` {self.value!r} (type {type(self.value).__name__}) cannot match "
                f"the {self.ref.field!r} field; it would always evaluate false"
            )
        # An empty `all_of`/`any_of` would validate (an empty tuple is not None)
        # and evaluate vacuously (all([]) is true, any([]) is false), silently
        # opening or closing the gate. Reject it: a combinator must combine at
        # least one sub-predicate (#74).
        if self.all_of is not None and len(self.all_of) == 0:
            raise ValueError("`all_of` must contain at least one sub-predicate, not be empty")
        if self.any_of is not None and len(self.any_of) == 0:
            raise ValueError("`any_of` must contain at least one sub-predicate, not be empty")
        if (
            is_leaf
            and self.op == "contains"
            and self.ref is not None
            and self.ref.field not in _STRING_PREDICATE_FIELDS
        ):
            raise ValueError(f"`contains` is valid only on a string field, not {self.ref.field!r}")
        return self

    def leaf_refs(self) -> tuple[PredicateRef, ...]:
        """Every leaf ``ref`` in this predicate tree, depth-first (#7).

        The single source the dependency and field invariants walk: a leaf
        contributes its own ``ref``; a combinator contributes the refs of its
        children recursively. So a ref buried in any combinator is checked. The
        depth-first walk is expressed as a ``fold`` (#77), so the shape dispatch
        lives once in ``fold`` rather than re-implemented here.
        """

        def _concat(children: list[tuple[PredicateRef, ...]]) -> tuple[PredicateRef, ...]:
            return tuple(ref for child in children for ref in child)

        return self.fold(
            leaf=lambda node: (node.ref,) if node.ref is not None else (),
            all_of=_concat,
            any_of=_concat,
            not_=lambda child: child,
        )


class Node(BaseModel):
    """A unit of work in a Workflow; v0.1 supports shell and agent Nodes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    kind: Literal["shell", "agent"]
    inputs: ShellNodeInputs | AgentNodeInputs
    needs: tuple[str, ...] = ()
    # A node-level `when` predicate gates whether the Node runs (#7): a false
    # predicate marks the Node `skipped` without executing it. `when` is the ONLY
    # conditional mechanism (CONTEXT.md); it adds no Edges, so the acyclic IR
    # (ADR 0002) stays honest.
    when: Predicate | None = None
    # The join policy axis (#7), separate from `when`: how this Node tolerates a
    # SKIPPED dependency. `all` (default) is today's behavior — any skipped
    # dependency skips this Node. `any` tolerates skipped upstream branches: the
    # Node runs iff at least one dependency executed and succeeded, and is itself
    # skipped only if ALL dependencies skipped. A FAILED dependency blocks
    # dependents regardless of join policy — join tolerates skips, never failures.
    join: Literal["all", "any"] = "all"
    # Failure-semantics policy lives per-Node (#6): `retries` counts the
    # ADDITIONAL Attempts the executor makes after the first on a retryable
    # failure, so total Attempts = retries + 1 and the default 0 keeps the
    # pre-#6 single-attempt behavior. `timeout` is a per-Node wall-clock budget
    # in seconds (gt=0 so a meaningless 0/negative budget fails validation
    # rather than silently disabling the timeout); None means no budget.
    retries: int = Field(default=0, ge=0)
    timeout: float | None = Field(default=None, gt=0)

    @model_validator(mode="before")
    @classmethod
    def _build_inputs_from_kind(cls, data: object, info: ValidationInfo) -> object:
        # `kind` lives at the node level in workflow definitions and is the single
        # source of truth: build the inputs model the node-level `kind` names,
        # rather than letting `inputs` pick its own variant independently (#62).
        # An explicit `inputs.kind` that disagrees with the node `kind` is a
        # mismatch — rejected as a config error instead of validating to a node
        # whose graph label and runner disagree.
        if isinstance(data, dict):
            kind = data.get("kind")
            inputs = data.get("inputs")
            if isinstance(kind, str) and isinstance(inputs, dict):
                explicit = inputs.get("kind")
                if explicit is not None and explicit != kind:
                    raise ValueError(
                        f"node kind {kind!r} disagrees with inputs.kind {explicit!r}; "
                        f"the node-level kind is authoritative"
                    )
                model = _INPUTS_MODEL_FOR_KIND.get(kind)
                if model is not None:
                    resolved = _resolve_inputs_paths({**inputs, "kind": kind}, info.context)
                    # Build the concrete inputs model the kind names and hand the
                    # built instance to the field. The field type is a plain union,
                    # so pydantic accepts the already-built instance as-is without
                    # re-resolving a variant — keeping validation errors on the
                    # authored field (inputs.command) with no discriminator tag.
                    data = {**data, "inputs": _build_inputs(model, resolved, info.context)}
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

    @model_validator(mode="after")
    def _when_refs_must_be_dependencies(self) -> "Node":
        # Every leaf `ref.node` in the predicate tree must be a dependency (#7):
        # `when` reads only what is guaranteed present at evaluation time, and it
        # adds no edges — so the IR stays acyclic (ADR 0002). A ref to a
        # non-dependency is a config error.
        if self.when is None:
            return self
        for ref in self.when.leaf_refs():
            if ref.node not in self.needs:
                raise ValueError(
                    f"node {self.id!r} `when` references {ref.node!r}, which is not in its needs"
                )
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
    def _when_refs_must_match_dependency_kind(cls, nodes: tuple[Node, ...]) -> tuple[Node, ...]:
        # Kind-aware `when` reference validation (#75): a leaf reads a field off a
        # dependency's normalized output, but only an agent Node ever emits
        # `structured_output` — a shell Node never does. The per-Node
        # `_when_refs_must_be_dependencies` validator only knows the dependency's
        # id, not its kind; here the whole node set is in scope, so an impossible
        # reference (e.g. `structured_output` of a shell dependency) is a config
        # error rather than a leaf that silently evaluates false on every run. This
        # runs after the known-reference validator, so every referenced node is
        # present in `kind_of`.
        kind_of = {node.id: node.kind for node in nodes}
        for node in nodes:
            if node.when is None:
                continue
            for ref in node.when.leaf_refs():
                producible = _PRODUCIBLE_FIELDS_FOR_KIND.get(
                    kind_of.get(ref.node, ""), frozenset()
                )
                if ref.field not in producible:
                    raise ValueError(
                        f"node {node.id!r} `when` references {ref.field!r} of {ref.node!r}, "
                        f"a {kind_of.get(ref.node)!r} node that does not produce it"
                    )
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


def _render_location(loc: tuple[int | str, ...], raw: dict[str, Any]) -> str:
    # The node-level kind selects the inputs model directly (#62), so no
    # discriminated-union tag is ever injected into the path: the location reads
    # `inputs.command` straight off the error, with no strip step.
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


def normalize_workflow(
    raw: dict[str, Any],
    source: str,
    *,
    base_dir: Path | None = None,
    known_adapters: frozenset[str] | None = None,
) -> Workflow:
    """Normalize a raw workflow mapping into the Workflow IR, or fail with field paths.

    ``base_dir`` anchors relative agent-Node ``output_schema`` / ``fixture`` paths
    (the workflow file's directory), so the same definition runs identically from
    any working directory (#64); when ``None`` the paths are left as authored.
    ``known_adapters`` overrides the built-in adapter set the adapter-known check
    validates against, for a caller injecting adapters at run time; when ``None``
    the built-in set applies (#64).
    """
    # A `pattern:` block compiles to plain `nodes:` BEFORE validation (ADR 0008),
    # so what is validated, checksummed, inspected, and run is an ordinary
    # Workflow — identical to the hand-authored `nodes:` equivalent.
    raw = expand_pattern(raw, source)
    context: dict[str, Any] = {}
    if base_dir is not None:
        context["base_dir"] = base_dir
    if known_adapters is not None:
        context["known_adapters"] = known_adapters
    try:
        return Workflow.model_validate(raw, context=context or None)
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
