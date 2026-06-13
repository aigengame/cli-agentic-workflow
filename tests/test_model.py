"""Model-seam tests: Workflow IR validation details exercised through normalize_workflow."""

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
