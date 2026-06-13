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


def evaluate_predicate(
    predicate: Predicate, output_of: Callable[[str], dict[str, Any] | None]
) -> bool:
    """Whether ``predicate`` holds, reading upstream outputs via ``output_of``.

    ``output_of(node_id)`` returns the named Node's normalized output mapping
    (``exit_status`` / ``stdout`` / optional ``structured_output``), or ``None``
    when the referenced Node produced NO output because it was SKIPPED (#74). A
    leaf over a skipped upstream evaluates false, which folds through all_of /
    any_of / not like any other false leaf, so a `when` referencing a skipped
    dependency is evaluated normally rather than crashing the Run.
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


def _evaluate_leaf(
    predicate: Predicate, output_of: Callable[[str], dict[str, Any] | None]
) -> bool:
    """Compare one referenced upstream field against the leaf's value (#7)."""
    assert predicate.ref is not None, "caller guarantees a leaf"
    output = output_of(predicate.ref.node)
    # A leaf over a SKIPPED upstream (no output) or over a field absent from the
    # output is FALSE for BOTH ops (#74). Guard BEFORE op dispatch so `contains`
    # never falls through to `str(value) in str(None)`, which would spuriously
    # match a substring of the literal text "None".
    if output is None or predicate.ref.field not in output:
        return False
    actual = output.get(predicate.ref.field)
    # `echo X` yields stdout "X\n", stored verbatim, so comparing against the
    # trailing newline(s) before the op makes an `equals`/`contains` on stdout
    # match a node that echoes the value (#74). Only the comparison strips; the
    # stored stdout in State and artifacts is left untouched.
    if predicate.ref.field == "stdout" and isinstance(actual, str):
        actual = actual.rstrip("\n")
    if predicate.op == "equals":
        # Python evaluates `0 == False` / `1 == True`, so a bool and an int (or
        # str) would spuriously match. When exactly one side is a bool, the leaf
        # is false; otherwise compare normally (#74).
        if isinstance(actual, bool) != isinstance(predicate.value, bool):
            return False
        return bool(actual == predicate.value)
    # `contains` is validated to a string field only, so `actual` is a string and
    # `value` is the substring tested for membership.
    assert predicate.op == "contains", "v0.1 ops are equals and contains"
    return str(predicate.value) in str(actual)
