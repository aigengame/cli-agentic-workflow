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

# Sentinel for "the addressed sub-path does not exist", distinct from a JSON
# ``null`` actually present at that path. An absent sub-path makes the leaf false
# (like an absent top-level field), never a spurious match.
_ABSENT = object()


def _address_sub_path(value: object, path: tuple[str | int, ...]) -> object:
    """Descend ``value`` along a structured_output sub-``path``, or ``_ABSENT`` (#75).

    Each step is a dict key (``str``) or a list index (``int``); a step that does
    not apply — a key missing from a mapping, an index out of range, or a step
    into a scalar — yields ``_ABSENT`` so the leaf evaluates false rather than
    raising. This is the addressing mechanism #13's classify-and-act routes on.
    """
    current: object = value
    for step in path:
        if isinstance(step, bool):
            # A bool is an int in Python, but it is never a meaningful list index;
            # treat it as a non-applicable step rather than indexing with 0/1.
            return _ABSENT
        if isinstance(step, str) and isinstance(current, dict):
            if step not in current:
                return _ABSENT
            current = current[step]
        elif isinstance(step, int) and isinstance(current, list):
            if not -len(current) <= step < len(current):
                return _ABSENT
            current = current[step]
        else:
            return _ABSENT
    return current


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

    The recursion is a ``Predicate.fold`` (#77): the leaf callback evaluates one
    comparison and the combinator callbacks AND / OR / negate their already-folded
    children, so shape dispatch lives once in ``fold`` rather than here.
    """
    return predicate.fold(
        leaf=lambda node: _evaluate_leaf(node, output_of),
        all_of=all,
        any_of=any,
        not_=lambda child: not child,
    )


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
    # A `structured_output` ref may address a sub-path INTO the parsed object
    # (#75); descend to the addressed scalar before comparing. An absent sub-path
    # (a missing key / out-of-range index / a step into a scalar) makes the leaf
    # false, exactly like an absent top-level field — never a spurious match.
    if predicate.ref.field == "structured_output" and predicate.ref.path:
        actual = _address_sub_path(actual, predicate.ref.path)
        if actual is _ABSENT:
            return False
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
