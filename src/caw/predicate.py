"""Evaluate a node-level `when` Predicate against upstream Node outputs (#7).

The predicate algebra is modelled structurally in ``caw.model`` (a leaf XOR a
combinator, validated there); this module is the run-time evaluator. It reads a
leaf's referenced field off the upstream Node's normalized output — the same
``{exit_status, stdout, structured_output}`` shape State persists — and folds
combinators recursively. There is no string eval and no parser: the evaluator
walks the validated model directly, so the only conditional surface is the typed
algebra.
"""

from collections.abc import Callable
from typing import Any

from caw.model import Predicate


def evaluate_predicate(predicate: Predicate, output_of: Callable[[str], dict[str, Any]]) -> bool:
    """Whether ``predicate`` holds, reading upstream outputs via ``output_of``.

    ``output_of(node_id)`` returns the named Node's normalized output mapping
    (``exit_status`` / ``stdout`` / optional ``structured_output``). Every leaf
    ref.node is a validated dependency, so the upstream output is guaranteed
    present when the predicate is evaluated (#7).
    """
    if predicate.ref is not None:
        return _evaluate_leaf(predicate, output_of)
    if predicate.all_of is not None:
        return all(evaluate_predicate(child, output_of) for child in predicate.all_of)
    if predicate.any_of is not None:
        return any(evaluate_predicate(child, output_of) for child in predicate.any_of)
    # The model guarantees exactly one shape, so a non-leaf, non-all/any predicate
    # is a `not`; mypy needs the explicit None guard.
    assert predicate.not_ is not None, "a non-leaf predicate is exactly one combinator"
    return not evaluate_predicate(predicate.not_, output_of)


def _evaluate_leaf(predicate: Predicate, output_of: Callable[[str], dict[str, Any]]) -> bool:
    """Compare one referenced upstream field against the leaf's value (#7)."""
    assert predicate.ref is not None, "caller guarantees a leaf"
    output = output_of(predicate.ref.node)
    actual = output.get(predicate.ref.field)
    if predicate.op == "equals":
        return bool(actual == predicate.value)
    # `contains` is validated to a string field only, so `actual` is a string and
    # `value` is the substring tested for membership.
    assert predicate.op == "contains", "v0.1 ops are equals and contains"
    return str(predicate.value) in str(actual)
