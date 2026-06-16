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


# A runnable `classify-and-act` example (#13): a classifier agent labels the input,
# then `when`-gated branches act on the label (the sole conditional mechanism — each
# branch reads the classifier's `structured_output.category`, ADR 0007 / #75), and a
# `join: any` report fans in whichever branch ran. The classifier fixture labels this
# input `bug`, so the bug branch runs, the feature branch skips, and the report runs
# on the one taken branch — all offline with the mock Adapter.
_CLASSIFY_AND_ACT_WORKFLOW = """\
# A runnable `classify-and-act` pattern example: a classifier labels the input, then
# `when`-gated branches act on the label and a `join: any` report fans in whichever
# branch ran. Inspect the expanded workflow with `caw graph classify-and-act.yaml`
# and run it with `caw run classify-and-act.yaml` — the `mock` adapter replays each
# fixture, so no real Agent CLI is required. The classifier fixture labels this input
# `bug`, so the bug branch runs and the feature branch skips.
name: classify-and-act-example
version: 1
pattern:
  type: classify-and-act
  classifier:
    id: classify
    kind: agent
    inputs:
      adapter: mock
      prompt: Classify the issue as a bug or a feature.
      fixture: classify.fixture.json
  branches:
    - id: handle-bug
      kind: agent
      when:
        ref:
          node: classify
          field: structured_output
          path: [category]
        op: equals
        value: bug
      inputs:
        adapter: mock
        prompt: Triage and fix the bug.
        fixture: handle-bug.fixture.json
    - id: handle-feature
      kind: agent
      when:
        ref:
          node: classify
          field: structured_output
          path: [category]
        op: equals
        value: feature
      inputs:
        adapter: mock
        prompt: Scope the feature request.
        fixture: handle-feature.fixture.json
  join:
    id: report
    kind: agent
    join: any
    inputs:
      adapter: mock
      prompt: Report the action taken.
      fixture: report.fixture.json
"""


# A runnable `generate-and-filter` example (#13): two candidate generators run
# concurrently, then a filter keeps the accepted ones. Every node replays a fixture,
# so it runs offline with the mock Adapter.
_GENERATE_AND_FILTER_WORKFLOW = """\
# A runnable `generate-and-filter` pattern example: two candidate generators run
# concurrently, then a `filter` agent keeps the accepted candidates. Inspect the
# expanded workflow with `caw graph generate-and-filter.yaml` and run it with
# `caw run generate-and-filter.yaml` — the `mock` adapter replays each fixture, so
# no real Agent CLI is required.
name: generate-and-filter-example
version: 1
pattern:
  type: generate-and-filter
  generators:
    - id: candidate-1
      kind: agent
      inputs:
        adapter: mock
        prompt: Propose a first candidate solution.
        fixture: candidate-1.fixture.json
    - id: candidate-2
      kind: agent
      inputs:
        adapter: mock
        prompt: Propose a second candidate solution.
        fixture: candidate-2.fixture.json
  filter:
    id: accept
    kind: agent
    inputs:
      adapter: mock
      prompt: Score the candidates and emit the accepted ones.
      fixture: accept.fixture.json
"""


# A runnable `fan-out-synthesis` example (#13): two workers research independent
# angles concurrently, then a synthesize node fans their results into one output.
# Every node replays a fixture, so it runs offline with the mock Adapter.
_FAN_OUT_SYNTHESIS_WORKFLOW = """\
# A runnable `fan-out-synthesis` pattern example: two workers research independent
# angles concurrently, then a `synthesize` agent fans their results into one output.
# Inspect the expanded workflow with `caw graph fan-out-synthesis.yaml` and run it
# with `caw run fan-out-synthesis.yaml` — the `mock` adapter replays each fixture,
# so no real Agent CLI is required.
name: fan-out-synthesis-example
version: 1
pattern:
  type: fan-out-synthesis
  workers:
    - id: angle-a
      kind: agent
      inputs:
        adapter: mock
        prompt: Research the problem from angle A.
        fixture: angle-a.fixture.json
    - id: angle-b
      kind: agent
      inputs:
        adapter: mock
        prompt: Research the problem from angle B.
        fixture: angle-b.fixture.json
  synthesize:
    id: synthesize
    kind: agent
    inputs:
      adapter: mock
      prompt: Synthesize the angles into one recommendation.
      fixture: synthesize.fixture.json
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
# The node whose normalized output the done Predicate and feedback source read.
evaluate_node: verdict
# The done Predicate reuses the `when` Predicate algebra (the sole conditional
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
    "classify-and-act": PatternExample(
        workflow_filename="classify-and-act.yaml",
        files={
            "classify-and-act.yaml": _CLASSIFY_AND_ACT_WORKFLOW,
            # The classifier labels this input `bug`, so the bug branch's `when`
            # holds and the feature branch's skips — the `join: any` report then
            # runs on the one taken branch.
            "classify.fixture.json": _fixture('{"category": "bug"}'),
            "handle-bug.fixture.json": _fixture('{"action": "fix queued"}'),
            "handle-feature.fixture.json": _fixture('{"action": "scoped"}'),
            "report.fixture.json": _fixture('{"reported": true}'),
        },
    ),
    "generate-and-filter": PatternExample(
        workflow_filename="generate-and-filter.yaml",
        files={
            "generate-and-filter.yaml": _GENERATE_AND_FILTER_WORKFLOW,
            "candidate-1.fixture.json": _fixture('{"idea": "approach one"}'),
            "candidate-2.fixture.json": _fixture('{"idea": "approach two"}'),
            "accept.fixture.json": _fixture('{"accepted": ["approach one"]}'),
        },
    ),
    "fan-out-synthesis": PatternExample(
        workflow_filename="fan-out-synthesis.yaml",
        files={
            "fan-out-synthesis.yaml": _FAN_OUT_SYNTHESIS_WORKFLOW,
            "angle-a.fixture.json": _fixture('{"finding": "from A"}'),
            "angle-b.fixture.json": _fixture('{"finding": "from B"}'),
            "synthesize.fixture.json": _fixture('{"recommendation": "combine A and B"}'),
        },
    ),
}
