"""Workflow scaffolding: the starter file and the per-pattern runnable examples (#8).

``caw init`` writes a minimal starter Workflow; ``caw patterns init <name>`` writes
a COMPLETE, runnable example of a built-in pattern (not an abstract template) that
``caw validate`` accepts and ``caw run`` runs to success offline. The pattern
examples are keyed by the registry's expander names, so #13's new expanders extend
this map alongside their registration rather than editing a dispatch.

Each example is authored with the ``pattern:`` surface (ADR 0008) so the scaffold
itself demonstrates the pattern's shape, and uses the offline mock Adapter (a
``claude.print`` agent node would need a real CLI) so a scaffolded run succeeds
with no Agent CLI installed.
"""

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


# A runnable `pipeline` example: ordered shell steps the expander chains linearly.
# Authored with the `pattern:` surface so the scaffold shows the pipeline's shape.
_PIPELINE_EXAMPLE = """\
# A runnable `pipeline` pattern example: ordered steps chained build -> test ->
# deploy. Inspect the expanded DAG with `caw graph pipeline.yaml` and run it with
# `caw run pipeline.yaml` — no Agent CLI required (the steps are shell nodes).
name: pipeline-example
version: 1
pattern:
  type: pipeline
  steps:
    - id: build
      kind: shell
      inputs:
        command: echo "building"
    - id: test
      kind: shell
      inputs:
        command: echo "testing"
    - id: deploy
      kind: shell
      inputs:
        command: echo "deploying"
"""


# A runnable `parallel` example: two independent shell branches joined downstream.
# Inspect with `caw graph parallel.yaml`; run with `caw run parallel.yaml`.
_PARALLEL_EXAMPLE = """\
# A runnable `parallel` pattern example: two independent branches run concurrently,
# then a `merge` node fans them in. Inspect the expanded DAG with
# `caw graph parallel.yaml` and run it with `caw run parallel.yaml`.
name: parallel-example
version: 1
pattern:
  type: parallel
  branches:
    - id: lint
      kind: shell
      inputs:
        command: echo "linting"
    - id: typecheck
      kind: shell
      inputs:
        command: echo "type-checking"
  join:
    id: merge
    kind: shell
    inputs:
      command: echo "merging results"
"""


# Pattern name -> (scaffolded example content, default filename). Keyed by the
# registry's expander names; #13 extends this map beside its registration.
PATTERN_EXAMPLES: dict[str, tuple[str, str]] = {
    "pipeline": (_PIPELINE_EXAMPLE, "pipeline.yaml"),
    "parallel": (_PARALLEL_EXAMPLE, "parallel.yaml"),
}
