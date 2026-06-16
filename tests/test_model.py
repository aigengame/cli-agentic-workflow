"""Model-seam tests: Workflow IR validation details exercised through normalize_workflow."""

import time
from typing import Any

import pytest

from caw.config import WorkflowConfigError
from caw.model import Node, ShellNodeInputs, Workflow, execution_order, normalize_workflow


def shell_node(node_id: str, *needs: str) -> Node:
    return Node(
        id=node_id, kind="shell", inputs=ShellNodeInputs(command="echo hi"), needs=tuple(needs)
    )


def test_cycle_error_names_only_the_cycle_members_not_downstream_nodes() -> None:
    raw: dict[str, Any] = {
        "name": "sample",
        "version": 1,
        "nodes": [
            {"id": "tail", "kind": "shell", "needs": ["a"], "inputs": {"command": "echo tail"}},
            {"id": "a", "kind": "shell", "needs": ["b"], "inputs": {"command": "echo a"}},
            {"id": "b", "kind": "shell", "needs": ["a"], "inputs": {"command": "echo b"}},
        ],
    }

    with pytest.raises(WorkflowConfigError) as excinfo:
        normalize_workflow(raw, source="workflow.yaml")

    message = str(excinfo.value)
    assert "dependency cycle: 'a' -> 'b' -> 'a'" in message
    assert "tail" not in message, "a node downstream of the cycle is not a cycle member"


def test_execution_order_breaks_ties_among_ready_nodes_by_declaration_order() -> None:
    # The order function's tie-break contract at its own unit seam: among nodes
    # whose dependencies are all satisfied, declaration order decides. caw graph
    # relies on this; the executor seam only pins the durable join-after-branches
    # contract, leaving the deterministic tie-break to be pinned here.
    workflow = Workflow(
        name="sample",
        version=1,
        nodes=(
            shell_node("join", "left", "right"),
            shell_node("left"),
            shell_node("right"),
        ),
    )

    ordered_ids = [node.id for node in execution_order(workflow)]

    assert ordered_ids == ["left", "right", "join"], (
        "left and right are independent and both ready first; declaration order "
        "(left before right) breaks the tie deterministically"
    )


def test_execution_order_raises_on_an_unpeelable_remainder_instead_of_a_partial_order() -> None:
    # A validation-bypassing constructor (model_construct, model_copy(update=...))
    # can hold a cyclic graph; ordering it must fail loudly, never return a
    # partial order that an executor would record as a vacuously succeeded Run.
    workflow = Workflow.model_construct(
        name="sample",
        version=1,
        nodes=(shell_node("before"), shell_node("a", "b"), shell_node("b", "a")),
    )

    with pytest.raises(ValueError) as excinfo:
        execution_order(workflow)

    message = str(excinfo.value)
    assert "'a'" in message and "'b'" in message, "the unorderable nodes are named"
    assert "'before'" not in message, "orderable nodes are not blamed"


def test_cycle_extraction_reports_an_invariant_breach_as_value_error_not_stop_iteration() -> None:
    # If a remainder node references only nodes outside the remainder (an
    # unknown reference that escaped earlier validation), cycle extraction must
    # raise ValueError — which pydantic converts into a normal validation
    # error — never StopIteration, which would escape pydantic unwrapped.
    from caw.model import _find_cycle

    dangling = [shell_node("a", "missing")]

    with pytest.raises(ValueError, match="'a'"):
        _find_cycle(dangling)


def test_error_location_renders_index_and_quoted_id_for_an_integer_like_id() -> None:
    raw: dict[str, Any] = {
        "name": "sample",
        "version": 1,
        "nodes": [{"id": "2", "kind": "shell", "inputs": {"command": "  "}}],
    }

    with pytest.raises(WorkflowConfigError) as excinfo:
        normalize_workflow(raw, source="workflow.yaml")

    assert "nodes[0 '2'].inputs.command" in str(excinfo.value), (
        "the location pairs the position with the quoted id, so '2' cannot read as an index"
    )


def test_node_kind_shell_with_an_explicit_agent_inputs_kind_is_a_config_error() -> None:
    # #62: node kind is the single source of truth. A node declaring kind `shell`
    # but carrying an explicit `inputs.kind: agent` must be rejected as a config
    # error, never validate to a shell-labelled node the executor runs as an agent.
    raw: dict[str, Any] = {
        "name": "sample",
        "version": 1,
        "nodes": [
            {
                "id": "n",
                "kind": "shell",
                "inputs": {"kind": "agent", "adapter": "mock", "prompt": "p"},
            }
        ],
    }

    with pytest.raises(WorkflowConfigError) as excinfo:
        normalize_workflow(raw, source="workflow.yaml")

    message = str(excinfo.value)
    assert "n" in message, "the error names the offending node"
    assert "kind" in message, "the error names the kind/inputs mismatch"


def test_node_kind_agent_with_an_explicit_shell_inputs_kind_is_a_config_error() -> None:
    # The reverse: kind `agent` with an explicit `inputs.kind: shell`.
    raw: dict[str, Any] = {
        "name": "sample",
        "version": 1,
        "nodes": [
            {
                "id": "n",
                "kind": "agent",
                "inputs": {"kind": "shell", "command": "echo hi"},
            }
        ],
    }

    with pytest.raises(WorkflowConfigError) as excinfo:
        normalize_workflow(raw, source="workflow.yaml")

    assert "kind" in str(excinfo.value), "the error names the kind/inputs mismatch"


def test_error_location_disambiguates_duplicate_ids_by_position() -> None:
    raw: dict[str, Any] = {
        "name": "sample",
        "version": 1,
        "nodes": [
            {"id": "greet", "kind": "shell", "inputs": {"command": "echo hi"}},
            {"id": "greet", "kind": "rocket", "inputs": {"command": "echo hi"}},
        ],
    }

    with pytest.raises(WorkflowConfigError) as excinfo:
        normalize_workflow(raw, source="workflow.yaml")

    assert "nodes[1 'greet'].kind" in str(excinfo.value), (
        "under duplicate ids only the position distinguishes the offending node"
    )


def test_cycle_message_quotes_ids_so_control_characters_cannot_break_the_one_line_contract() -> (
    None
):
    sneaky = "a\nb"
    raw: dict[str, Any] = {
        "name": "sample",
        "version": 1,
        "nodes": [
            {"id": sneaky, "kind": "shell", "needs": ["c"], "inputs": {"command": "echo hi"}},
            {"id": "c", "kind": "shell", "needs": [sneaky], "inputs": {"command": "echo hi"}},
        ],
    }

    with pytest.raises(WorkflowConfigError) as excinfo:
        normalize_workflow(raw, source="workflow.yaml")

    message = str(excinfo.value)
    assert "dependency cycle" in message
    assert "\n" not in message, "a newline-bearing id must not break the one-error-line contract"
    assert "'a\\nb'" in message, "ids are quoted with their control characters escaped"


def test_cycle_message_arrows_point_in_execution_direction_like_the_json_plan_edges() -> None:
    # a needs b, b needs c, c needs a. The JSON plan renders edges from
    # dependency to dependent; the cycle message uses the same convention,
    # so "x -> y" always means "x runs before y".
    raw: dict[str, Any] = {
        "name": "sample",
        "version": 1,
        "nodes": [
            {"id": "a", "kind": "shell", "needs": ["b"], "inputs": {"command": "echo a"}},
            {"id": "b", "kind": "shell", "needs": ["c"], "inputs": {"command": "echo b"}},
            {"id": "c", "kind": "shell", "needs": ["a"], "inputs": {"command": "echo c"}},
        ],
    }

    with pytest.raises(WorkflowConfigError) as excinfo:
        normalize_workflow(raw, source="workflow.yaml")

    assert "dependency cycle: 'a' -> 'c' -> 'b' -> 'a'" in str(excinfo.value)


def test_concurrency_defaults_to_a_conservative_limit_when_unspecified() -> None:
    # A workflow that does not declare concurrency runs at the kernel's
    # conservative default rather than unbounded or one-at-a-time.
    raw: dict[str, Any] = {
        "name": "sample",
        "version": 1,
        "nodes": [{"id": "greet", "kind": "shell", "inputs": {"command": "echo hi"}}],
    }

    workflow = normalize_workflow(raw, source="<test>")

    assert workflow.concurrency == 4


def test_concurrency_can_be_raised_in_workflow_config() -> None:
    raw: dict[str, Any] = {
        "name": "sample",
        "version": 1,
        "concurrency": 8,
        "nodes": [{"id": "greet", "kind": "shell", "inputs": {"command": "echo hi"}}],
    }

    workflow = normalize_workflow(raw, source="<test>")

    assert workflow.concurrency == 8


def test_concurrency_below_one_is_a_config_error() -> None:
    raw: dict[str, Any] = {
        "name": "sample",
        "version": 1,
        "concurrency": 0,
        "nodes": [{"id": "greet", "kind": "shell", "inputs": {"command": "echo hi"}}],
    }

    with pytest.raises(WorkflowConfigError) as excinfo:
        normalize_workflow(raw, source="workflow.yaml")

    assert "concurrency" in str(excinfo.value)


def test_retries_default_to_zero_so_a_node_runs_exactly_once_unless_asked_otherwise() -> None:
    # The retry policy is per-node and opt-in: with no `retries` declared a Node
    # is attempted exactly once (total attempts = retries + 1 = 1), preserving the
    # pre-#6 single-attempt behavior for every existing workflow.
    raw: dict[str, Any] = {
        "name": "sample",
        "version": 1,
        "nodes": [{"id": "greet", "kind": "shell", "inputs": {"command": "echo hi"}}],
    }

    workflow = normalize_workflow(raw, source="<test>")

    assert workflow.nodes[0].retries == 0
    assert workflow.nodes[0].timeout is None


def test_retries_records_additional_attempts_after_the_first() -> None:
    # `retries` is the number of ADDITIONAL attempts after the first, so a Node
    # with retries=2 may be attempted up to three times. The field carries the
    # policy; the executor enforces the count.
    raw: dict[str, Any] = {
        "name": "sample",
        "version": 1,
        "nodes": [
            {"id": "greet", "kind": "shell", "retries": 2, "inputs": {"command": "echo hi"}}
        ],
    }

    workflow = normalize_workflow(raw, source="<test>")

    assert workflow.nodes[0].retries == 2


def test_negative_retries_is_a_config_error() -> None:
    raw: dict[str, Any] = {
        "name": "sample",
        "version": 1,
        "nodes": [
            {"id": "greet", "kind": "shell", "retries": -1, "inputs": {"command": "echo hi"}}
        ],
    }

    with pytest.raises(WorkflowConfigError) as excinfo:
        normalize_workflow(raw, source="workflow.yaml")

    assert "retries" in str(excinfo.value)


def test_timeout_is_a_positive_wall_clock_second_budget() -> None:
    raw: dict[str, Any] = {
        "name": "sample",
        "version": 1,
        "nodes": [
            {"id": "greet", "kind": "shell", "timeout": 1.5, "inputs": {"command": "echo hi"}}
        ],
    }

    workflow = normalize_workflow(raw, source="<test>")

    assert workflow.nodes[0].timeout == 1.5


def test_a_non_positive_timeout_is_a_config_error() -> None:
    # A timeout is a wall-clock budget; zero or negative seconds is meaningless
    # and must be rejected at validation time rather than silently disabling it.
    raw: dict[str, Any] = {
        "name": "sample",
        "version": 1,
        "nodes": [
            {"id": "greet", "kind": "shell", "timeout": 0, "inputs": {"command": "echo hi"}}
        ],
    }

    with pytest.raises(WorkflowConfigError) as excinfo:
        normalize_workflow(raw, source="workflow.yaml")

    assert "timeout" in str(excinfo.value)


def test_a_node_accepts_a_leaf_when_predicate_referencing_an_upstream_node() -> None:
    # The atomic unit of the predicate algebra (#7): one reference -> comparison.
    # A node may carry a leaf `when` whose `ref.node` is an upstream dependency;
    # the normalized predicate round-trips on the frozen Node.
    raw: dict[str, Any] = {
        "name": "sample",
        "version": 1,
        "nodes": [
            {"id": "classify", "kind": "shell", "inputs": {"command": "echo billing"}},
            {
                "id": "act",
                "kind": "shell",
                "needs": ["classify"],
                "when": {
                    "ref": {"node": "classify", "field": "stdout"},
                    "op": "equals",
                    "value": "billing",
                },
                "inputs": {"command": "echo acting"},
            },
        ],
    }

    workflow = normalize_workflow(raw, source="<test>")

    act = workflow.nodes[1]
    assert act.when is not None, "the node carries a `when` predicate"
    assert act.when.ref is not None, "a leaf predicate carries a ref"
    assert act.when.ref.node == "classify"
    assert act.when.ref.field == "stdout"
    assert act.when.op == "equals"
    assert act.when.value == "billing"


def test_a_node_accepts_all_of_any_of_and_not_combinators_nesting_leaves() -> None:
    # Composability (#7): a predicate is recursively a leaf OR a combinator, and
    # combinators (all_of / any_of / not) nest arbitrarily. This exercises all
    # three combinators in one tree, with `not` wrapping a nested `any_of`.
    raw: dict[str, Any] = {
        "name": "sample",
        "version": 1,
        "nodes": [
            {"id": "a", "kind": "shell", "inputs": {"command": "echo x"}},
            {"id": "b", "kind": "shell", "inputs": {"command": "echo y"}},
            {
                "id": "gate",
                "kind": "shell",
                "needs": ["a", "b"],
                "when": {
                    "all_of": [
                        {"ref": {"node": "a", "field": "stdout"}, "op": "equals", "value": "x"},
                        {
                            "not": {
                                "any_of": [
                                    {
                                        "ref": {"node": "b", "field": "exit_status"},
                                        "op": "equals",
                                        "value": 1,
                                    },
                                    {
                                        "ref": {"node": "b", "field": "stdout"},
                                        "op": "contains",
                                        "value": "skip",
                                    },
                                ]
                            }
                        },
                    ]
                },
                "inputs": {"command": "echo gate"},
            },
        ],
    }

    workflow = normalize_workflow(raw, source="<test>")

    gate = workflow.nodes[2]
    assert gate.when is not None
    assert gate.when.all_of is not None and len(gate.when.all_of) == 2
    inner_not = gate.when.all_of[1]
    assert inner_not.not_ is not None
    assert inner_not.not_.any_of is not None and len(inner_not.not_.any_of) == 2


def test_a_when_referencing_a_node_not_in_needs_is_a_config_error() -> None:
    # The recursive validation invariant (#7): every leaf `ref.node` in the
    # predicate tree must appear in the owning Node's `needs`, so the referenced
    # output is guaranteed present at evaluation time and `when` adds no edges
    # (ADR 0002). Here `act` references `classify` but does not depend on it.
    raw: dict[str, Any] = {
        "name": "sample",
        "version": 1,
        "nodes": [
            {"id": "classify", "kind": "shell", "inputs": {"command": "echo billing"}},
            {
                "id": "act",
                "kind": "shell",
                "needs": [],
                "when": {
                    "ref": {"node": "classify", "field": "stdout"},
                    "op": "equals",
                    "value": "billing",
                },
                "inputs": {"command": "echo acting"},
            },
        ],
    }

    with pytest.raises(WorkflowConfigError) as excinfo:
        normalize_workflow(raw, source="workflow.yaml")

    message = str(excinfo.value)
    assert "classify" in message, "the error names the referenced non-dependency"
    assert "needs" in message, "the error names the invariant it breached"


def test_a_when_combinator_referencing_a_node_not_in_needs_is_a_config_error() -> None:
    # The invariant collects EVERY leaf ref in the tree, including ones nested
    # deep inside combinators: here the offending ref hides inside an all_of/not.
    raw: dict[str, Any] = {
        "name": "sample",
        "version": 1,
        "nodes": [
            {"id": "a", "kind": "shell", "inputs": {"command": "echo x"}},
            {"id": "stranger", "kind": "shell", "inputs": {"command": "echo y"}},
            {
                "id": "gate",
                "kind": "shell",
                "needs": ["a"],
                "when": {
                    "all_of": [
                        {"ref": {"node": "a", "field": "stdout"}, "op": "equals", "value": "x"},
                        {
                            "not": {
                                "ref": {"node": "stranger", "field": "stdout"},
                                "op": "equals",
                                "value": "y",
                            }
                        },
                    ]
                },
                "inputs": {"command": "echo gate"},
            },
        ],
    }

    with pytest.raises(WorkflowConfigError) as excinfo:
        normalize_workflow(raw, source="workflow.yaml")

    assert "stranger" in str(excinfo.value), "a ref buried in a combinator is still checked"


def test_a_predicate_mixing_a_leaf_and_a_combinator_is_a_config_error() -> None:
    # Shape exclusivity (#7): a predicate is EXACTLY one shape — a leaf XOR one
    # combinator. A predicate carrying both leaf fields and a combinator is
    # ambiguous and rejected as a config error.
    raw: dict[str, Any] = {
        "name": "sample",
        "version": 1,
        "nodes": [
            {"id": "a", "kind": "shell", "inputs": {"command": "echo x"}},
            {
                "id": "gate",
                "kind": "shell",
                "needs": ["a"],
                "when": {
                    "ref": {"node": "a", "field": "stdout"},
                    "op": "equals",
                    "value": "x",
                    "all_of": [
                        {"ref": {"node": "a", "field": "stdout"}, "op": "equals", "value": "x"}
                    ],
                },
                "inputs": {"command": "echo gate"},
            },
        ],
    }

    with pytest.raises(WorkflowConfigError) as excinfo:
        normalize_workflow(raw, source="workflow.yaml")

    assert "when" in str(excinfo.value), "the error names the malformed predicate"


def test_an_empty_predicate_is_a_config_error() -> None:
    # A predicate that is neither a valid leaf nor any combinator (an empty
    # mapping) is a config error: it has no shape at all.
    raw: dict[str, Any] = {
        "name": "sample",
        "version": 1,
        "nodes": [
            {"id": "a", "kind": "shell", "inputs": {"command": "echo x"}},
            {
                "id": "gate",
                "kind": "shell",
                "needs": ["a"],
                "when": {},
                "inputs": {"command": "echo gate"},
            },
        ],
    }

    with pytest.raises(WorkflowConfigError) as excinfo:
        normalize_workflow(raw, source="workflow.yaml")

    assert "when" in str(excinfo.value)


def test_a_leaf_predicate_missing_op_is_a_config_error() -> None:
    # A leaf must be complete: a `ref` with no `op` is not a valid leaf, and with
    # no combinator either it is not exactly one shape.
    raw: dict[str, Any] = {
        "name": "sample",
        "version": 1,
        "nodes": [
            {"id": "a", "kind": "shell", "inputs": {"command": "echo x"}},
            {
                "id": "gate",
                "kind": "shell",
                "needs": ["a"],
                "when": {"ref": {"node": "a", "field": "stdout"}, "value": "x"},
                "inputs": {"command": "echo gate"},
            },
        ],
    }

    with pytest.raises(WorkflowConfigError) as excinfo:
        normalize_workflow(raw, source="workflow.yaml")

    assert "when" in str(excinfo.value)


def test_two_combinators_at_once_is_a_config_error() -> None:
    # Exactly one combinator: a predicate carrying both all_of and any_of is two
    # shapes at once, rejected.
    raw: dict[str, Any] = {
        "name": "sample",
        "version": 1,
        "nodes": [
            {"id": "a", "kind": "shell", "inputs": {"command": "echo x"}},
            {
                "id": "gate",
                "kind": "shell",
                "needs": ["a"],
                "when": {
                    "all_of": [
                        {"ref": {"node": "a", "field": "stdout"}, "op": "equals", "value": "x"}
                    ],
                    "any_of": [
                        {"ref": {"node": "a", "field": "stdout"}, "op": "equals", "value": "y"}
                    ],
                },
                "inputs": {"command": "echo gate"},
            },
        ],
    }

    with pytest.raises(WorkflowConfigError) as excinfo:
        normalize_workflow(raw, source="workflow.yaml")

    assert "when" in str(excinfo.value)


def test_an_empty_all_of_combinator_is_a_config_error() -> None:
    # FIX 4 (#74): an empty `all_of` is not a meaningful conjunction — it would
    # validate (an empty tuple is `is not None`) and evaluate vacuously true,
    # silently opening the gate. Reject it at validation time instead.
    raw: dict[str, Any] = {
        "name": "sample",
        "version": 1,
        "nodes": [
            {"id": "a", "kind": "shell", "inputs": {"command": "echo x"}},
            {
                "id": "gate",
                "kind": "shell",
                "needs": ["a"],
                "when": {"all_of": []},
                "inputs": {"command": "echo gate"},
            },
        ],
    }

    with pytest.raises(WorkflowConfigError) as excinfo:
        normalize_workflow(raw, source="workflow.yaml")

    message = str(excinfo.value)
    assert "all_of" in message, "the error names the empty combinator"


def test_an_empty_any_of_combinator_is_a_config_error() -> None:
    # FIX 4 (#74): an empty `any_of` would validate and evaluate vacuously false,
    # silently closing the gate. Reject it at validation time.
    raw: dict[str, Any] = {
        "name": "sample",
        "version": 1,
        "nodes": [
            {"id": "a", "kind": "shell", "inputs": {"command": "echo x"}},
            {
                "id": "gate",
                "kind": "shell",
                "needs": ["a"],
                "when": {"any_of": []},
                "inputs": {"command": "echo gate"},
            },
        ],
    }

    with pytest.raises(WorkflowConfigError) as excinfo:
        normalize_workflow(raw, source="workflow.yaml")

    message = str(excinfo.value)
    assert "any_of" in message, "the error names the empty combinator"


def test_contains_on_a_non_string_field_is_a_config_error() -> None:
    # `contains` is a substring test, valid only on a string field (#7): using it
    # against `exit_status` (an integer) is a config error, caught at validation
    # time rather than producing a meaningless run-time comparison.
    raw: dict[str, Any] = {
        "name": "sample",
        "version": 1,
        "nodes": [
            {"id": "a", "kind": "shell", "inputs": {"command": "echo x"}},
            {
                "id": "gate",
                "kind": "shell",
                "needs": ["a"],
                "when": {
                    "ref": {"node": "a", "field": "exit_status"},
                    "op": "contains",
                    "value": 0,
                },
                "inputs": {"command": "echo gate"},
            },
        ],
    }

    with pytest.raises(WorkflowConfigError) as excinfo:
        normalize_workflow(raw, source="workflow.yaml")

    message = str(excinfo.value)
    assert "contains" in message, "the error names the misused operator"
    assert "exit_status" in message, "the error names the offending field"


def gate_workflow(when: dict[str, Any], *, upstream_kind: str = "shell") -> dict[str, Any]:
    """A two-node `classify -> gate` workflow whose gate carries the given `when`.

    The upstream `classify` is a shell Node by default; pass ``upstream_kind="agent"``
    for the kind-aware `structured_output` reference tests (an agent Node is the only
    kind that can emit `structured_output`). This keeps the value-type and
    structured-output validation tests free of repeated raw-workflow scaffolding.
    """
    if upstream_kind == "agent":
        classify: dict[str, Any] = {
            "id": "classify",
            "kind": "agent",
            "inputs": {"adapter": "mock", "prompt": "classify the ticket"},
        }
    else:
        classify = {"id": "classify", "kind": "shell", "inputs": {"command": "echo billing"}}
    return {
        "name": "sample",
        "version": 1,
        "nodes": [
            classify,
            {
                "id": "gate",
                "kind": "shell",
                "needs": ["classify"],
                "when": when,
                "inputs": {"command": "echo gate"},
            },
        ],
    }


def test_a_string_value_against_exit_status_is_a_config_error() -> None:
    # #75: `exit_status` is an integer field, so a STRING `value` can never match
    # it — `0 == "0"` is always false, silently skipping the gated node on every
    # run. Reject the type mismatch at validation time with an actionable message
    # rather than accepting an always-false gate.
    raw = gate_workflow(
        {"ref": {"node": "classify", "field": "exit_status"}, "op": "equals", "value": "0"}
    )

    with pytest.raises(WorkflowConfigError) as excinfo:
        normalize_workflow(raw, source="workflow.yaml")

    message = str(excinfo.value)
    assert "exit_status" in message, "the error names the integer field"
    assert "value" in message, "the error names the offending value"


def test_a_bool_value_against_exit_status_is_a_config_error() -> None:
    # #75: a `bool` value against `exit_status` is rejected at config time even
    # though Python aliases `True == 1` / `False == 0`. That aliasing is the
    # confusing always-or-never match #74 refused at eval time; here it is a config
    # error, so a bool-vs-exit_status gate never reaches the scheduler (this is the
    # config-time home of the former executor-seam bool-coercion case).
    raw = gate_workflow(
        {"ref": {"node": "classify", "field": "exit_status"}, "op": "equals", "value": False}
    )

    with pytest.raises(WorkflowConfigError) as excinfo:
        normalize_workflow(raw, source="workflow.yaml")

    message = str(excinfo.value)
    assert "exit_status" in message, "the error names the integer field"
    assert "bool" in message, "the error names the offending bool type"


def test_an_int_value_against_stdout_is_a_config_error() -> None:
    # #75: `stdout` is a string field, so an `int` value can never match it after
    # the leaf evaluator compares the (string) stdout against the value. Reject the
    # mismatch at config time rather than accepting an always-false gate.
    raw = gate_workflow(
        {"ref": {"node": "classify", "field": "stdout"}, "op": "equals", "value": 1}
    )

    with pytest.raises(WorkflowConfigError) as excinfo:
        normalize_workflow(raw, source="workflow.yaml")

    message = str(excinfo.value)
    assert "stdout" in message, "the error names the string field"


def test_a_leaf_missing_value_is_a_config_error() -> None:
    # #75 DECISION: `value` is REQUIRED for a leaf. A leaf with an `op` but no
    # `value` defaults `value` to None, which would silently become a
    # near-always-false gate (with `contains` it degrades to `'None' in actual`).
    # Reject the missing value at config time rather than accepting the silent gate.
    raw = gate_workflow({"ref": {"node": "classify", "field": "stdout"}, "op": "equals"})

    with pytest.raises(WorkflowConfigError) as excinfo:
        normalize_workflow(raw, source="workflow.yaml")

    message = str(excinfo.value)
    assert "value" in message, "the error names the missing value"


def test_an_explicit_equals_null_value_is_a_config_error() -> None:
    # #75 DECISION: `equals null` is NOT a supported comparison. No normalized
    # field is ever JSON null (exit_status is int, stdout is str; an absent
    # structured_output sub-path is ABSENCE, evaluating false, not a null match),
    # so an explicit `value: null` leaf could never meaningfully match — it is a
    # config error, indistinguishable in intent from a forgotten value.
    raw = gate_workflow(
        {"ref": {"node": "classify", "field": "stdout"}, "op": "equals", "value": None}
    )

    with pytest.raises(WorkflowConfigError) as excinfo:
        normalize_workflow(raw, source="workflow.yaml")

    message = str(excinfo.value)
    assert "value" in message, "the error names the unsupported null value"


def test_join_defaults_to_all_and_accepts_any() -> None:
    # The join policy axis (#7): a Node's `join` defaults to `all` (today's
    # behavior — any skipped dependency skips this Node) and may be set to `any`
    # (tolerate skipped upstream branches).
    raw: dict[str, Any] = {
        "name": "sample",
        "version": 1,
        "nodes": [
            {"id": "a", "kind": "shell", "inputs": {"command": "echo x"}},
            {"id": "b", "kind": "shell", "inputs": {"command": "echo y"}},
            {"id": "strict", "kind": "shell", "needs": ["a"], "inputs": {"command": "echo s"}},
            {
                "id": "tolerant",
                "kind": "shell",
                "needs": ["a", "b"],
                "join": "any",
                "inputs": {"command": "echo t"},
            },
        ],
    }

    workflow = normalize_workflow(raw, source="<test>")

    by_id = {node.id: node for node in workflow.nodes}
    assert by_id["strict"].join == "all", "join defaults to all"
    assert by_id["tolerant"].join == "any"


def test_an_unknown_join_policy_is_a_config_error() -> None:
    raw: dict[str, Any] = {
        "name": "sample",
        "version": 1,
        "nodes": [
            {
                "id": "gate",
                "kind": "shell",
                "join": "some",
                "inputs": {"command": "echo gate"},
            }
        ],
    }

    with pytest.raises(WorkflowConfigError) as excinfo:
        normalize_workflow(raw, source="workflow.yaml")

    assert "join" in str(excinfo.value)


def test_validation_and_ordering_scale_to_thousands_of_nodes() -> None:
    # Pattern Expanders (roadmap) compile patterns into graphs of exactly this
    # scale, and validate is sold as the fast fail-fast check. A 5,000-node
    # reverse-declared linear chain is the worst case for a quadratic peel
    # (declaration order is the exact reverse of dependency order); it must
    # validate and order in well under a second. The 5s threshold is generous
    # to avoid CI flakiness while still failing an accidental O(N^2) regression
    # (which costs ~15s here).
    count = 5000
    raw: dict[str, Any] = {
        "name": "sample",
        "version": 1,
        "nodes": [
            {
                "id": f"node{index}",
                "kind": "shell",
                "needs": [f"node{index - 1}"] if index > 1 else [],
                "inputs": {"command": "echo hi"},
            }
            for index in range(count, 0, -1)
        ],
    }

    start = time.perf_counter()
    workflow = normalize_workflow(raw, source="<test>")
    ordered = execution_order(workflow)
    elapsed = time.perf_counter() - start

    assert [node.id for node in ordered] == [f"node{index}" for index in range(1, count + 1)]
    assert elapsed < 5.0, (
        f"validation+ordering of {count} nodes took {elapsed:.2f}s (expected < 5s)"
    )
