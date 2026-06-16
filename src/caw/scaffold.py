"""Workflow scaffolding: the starter file and the per-pattern runnable examples (#8).

``caw init`` writes a minimal starter Workflow; ``caw patterns init <name>`` writes
a COMPLETE, runnable example of a built-in pattern (not an abstract template) that
``caw validate`` accepts and ``caw run`` runs to success offline. The pattern
examples are keyed by the registry's expander names, so #13's new expanders extend
this map alongside their registration rather than editing a dispatch.

Each example is authored with the ``pattern:`` surface (ADR 0008) so the scaffold
itself demonstrates the pattern's shape, and exercises the offline mock Adapter
(an agent Node whose ``adapter: mock`` replays a companion ``fixture`` file) so a
scaffolded run succeeds with no real Agent CLI installed — the meaningful agent
path, runnable today. A ``PatternExample`` is therefore a small bundle: the
workflow file plus any companion fixture files written beside it.
"""

from dataclasses import dataclass

# A minimal starter: one shell Node that validates and runs anywhere, offline.
STARTER_WORKFLOW = """\
# A minimal caw starter workflow. Validate it with `caw validate workflow.yaml`
# and run it with `caw run workflow.yaml`.
name: starter
version: 1
nodes:
  - id: greet
    kind: shell
    inputs:
      command: echo "hello from caw"
"""


@dataclass(frozen=True)
class PatternExample:
    """A complete, runnable scaffold for one pattern: a workflow + companion files.

    ``workflow_filename`` is the default name of the workflow file; ``files`` maps
    every file the example writes (the workflow itself plus any companion fixtures)
    to its content. ``caw patterns init`` writes the workflow under the chosen path
    and each companion beside it, so the scaffolded bundle runs as-is offline.
    """

    workflow_filename: str
    files: dict[str, str]


# A `mock` fixture is a canned normalized agent result the mock Adapter replays
# (exit_status + optional stdout / structured_output), so the example runs offline.
def _fixture(structured_output: str) -> str:
    return f'{{"exit_status": 0, "structured_output": {structured_output}}}\n'


# A runnable `pipeline` example: ordered mock-Adapter agent steps the expander
# chains linearly. Each step replays a companion fixture, demonstrating the agent
# path end to end with no real Agent CLI installed.
_PIPELINE_WORKFLOW = """\
# A runnable `pipeline` pattern example: three mock-Adapter agent steps chained
# draft -> review -> publish. Inspect the expanded workflow with
# `caw graph pipeline.yaml` and run it with `caw run pipeline.yaml` — the `mock`
# adapter replays each step's fixture, so no real Agent CLI is required.
name: pipeline-example
version: 1
pattern:
  type: pipeline
  steps:
    - id: draft
      kind: agent
      inputs:
        adapter: mock
        prompt: Draft a short release note.
        fixture: draft.fixture.json
    - id: review
      kind: agent
      inputs:
        adapter: mock
        prompt: Review the draft for clarity.
        fixture: review.fixture.json
    - id: publish
      kind: agent
      inputs:
        adapter: mock
        prompt: Produce the final release note.
        fixture: publish.fixture.json
"""


# A runnable `parallel` example: two independent mock-Adapter agent branches fanned
# in by a third. Inspect with `caw graph parallel.yaml`; run with
# `caw run parallel.yaml` — every node replays a companion fixture offline.
_PARALLEL_WORKFLOW = """\
# A runnable `parallel` pattern example: two independent mock-Adapter agent
# branches run concurrently, then a `merge` agent fans them in. Inspect the
# expanded workflow with `caw graph parallel.yaml` and run it with
# `caw run parallel.yaml` — the `mock` adapter replays each fixture, so no real
# Agent CLI is required.
name: parallel-example
version: 1
pattern:
  type: parallel
  branches:
    - id: research
      kind: agent
      inputs:
        adapter: mock
        prompt: Research approach A.
        fixture: research.fixture.json
    - id: critique
      kind: agent
      inputs:
        adapter: mock
        prompt: Critique approach A.
        fixture: critique.fixture.json
  join:
    id: merge
    kind: agent
    inputs:
      adapter: mock
      prompt: Synthesize the research and critique.
      fixture: merge.fixture.json
"""


# A runnable `loop-until-done` Pattern Controller example (#15, ADR 0009): a
# controller spec drives an iteration workflow until a done-predicate holds. The
# iteration is a single mock-Adapter agent Node whose fixture reports a verdict in
# `stdout` (CONTINUE / FINISHED) and points the next iteration at its fixture via
# `structured_output.next_fixture`; the controller's structural feedback substitutes
# that into the node's `fixture` field. iteration 1 reports CONTINUE -> iteration 2
# reports FINISHED, so the loop stops at iteration 2 — all offline, no Agent CLI.
_LOOP_SPEC = """\
# A runnable `loop-until-done` Pattern Controller example. Run it with
# `caw loop run loop.yaml` — the iteration is re-run with feedback until the
# `done` predicate holds (here: the `verdict` node's stdout contains FINISHED),
# the iteration's Run fails, or `max_iterations` is reached. Report the whole Run
# Group with `caw loop report <group-id>`.
workflow: loop-iteration.yaml
max_iterations: 5
# The node whose normalized output the done-predicate and feedback source read.
evaluate_node: verdict
# The stop condition reuses the `when` Predicate algebra (the sole conditional
# mechanism): done when the `verdict` node's stdout contains FINISHED.
done:
  ref:
    node: verdict
    field: stdout
  op: contains
  value: FINISHED
# Feedback from iteration N -> iteration N+1: the prior verdict's
# `structured_output.next_fixture` is substituted into the `verdict` node's
# `fixture` field for the next iteration (structural substitution, not templating).
feedback:
  to_node: verdict
  to_field: fixture
  from_field: next_fixture
"""

_LOOP_ITERATION_WORKFLOW = """\
# One loop iteration: a single mock-Adapter agent Node that emits a verdict. The
# controller re-runs this workflow with feedback until the done-predicate holds.
name: loop-until-done-iteration
version: 1
nodes:
  - id: verdict
    kind: agent
    inputs:
      adapter: mock
      prompt: Decide whether the task is done; if not, point to the next iteration.
      fixture: verdict-1.fixture.json
"""


def _loop_fixture(*, done: bool, next_fixture: str | None = None) -> str:
    """A loop-iteration fixture: a verdict in stdout + an optional next-fixture pointer."""
    structured = f'{{"next_fixture": "{next_fixture}"}}' if next_fixture else "{}"
    stdout = "FINISHED" if done else "CONTINUE"
    return f'{{"exit_status": 0, "stdout": "{stdout}", "structured_output": {structured}}}\n'


# The loop-until-done controller bundle: the spec, the iteration workflow, and the
# two iteration fixtures (CONTINUE -> FINISHED). Reuses the PatternExample bundle
# shape (a workflow file + companion files); `caw loop init` writes the whole bundle.
LOOP_EXAMPLE = PatternExample(
    workflow_filename="loop.yaml",
    files={
        "loop.yaml": _LOOP_SPEC,
        "loop-iteration.yaml": _LOOP_ITERATION_WORKFLOW,
        "verdict-1.fixture.json": _loop_fixture(done=False, next_fixture="verdict-2.fixture.json"),
        "verdict-2.fixture.json": _loop_fixture(done=True),
    },
)


# Pattern name -> its runnable scaffold bundle. Keyed by the registry's expander
# names; #13 extends this map beside its registration.
PATTERN_EXAMPLES: dict[str, PatternExample] = {
    "pipeline": PatternExample(
        workflow_filename="pipeline.yaml",
        files={
            "pipeline.yaml": _PIPELINE_WORKFLOW,
            "draft.fixture.json": _fixture('{"draft": "a first draft"}'),
            "review.fixture.json": _fixture('{"notes": "looks clear"}'),
            "publish.fixture.json": _fixture('{"published": true}'),
        },
    ),
    "parallel": PatternExample(
        workflow_filename="parallel.yaml",
        files={
            "parallel.yaml": _PARALLEL_WORKFLOW,
            "research.fixture.json": _fixture('{"approach": "A"}'),
            "critique.fixture.json": _fixture('{"risk": "low"}'),
            "merge.fixture.json": _fixture('{"decision": "proceed"}'),
        },
    ),
}
