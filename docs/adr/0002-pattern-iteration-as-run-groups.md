# ADR 0002: Pattern Iteration Materializes Immutable Runs in a Run Group

Status: Accepted
Date: 2026-06-11
Related: `docs/adr/0001-local-first-python-bash-workflow-kernel.md`,
`docs/prd/0001-cli-agentic-workflow.md`

ADR 0001 keeps each concrete Workflow IR acyclic but left iteration semantics open between
"repeated DAG runs" and "expanded DAG subgraphs". We decided on repeated runs: every pattern
iteration is a separate Run whose graph is immutable once execution starts. A Pattern
Controller evaluates the finished Run N, passes feedback as inputs to a newly materialized
Run N+1, and links successive Runs into a Run Group. The Run Group is the unit of aggregate
reporting and resumption for iterative patterns; controller state (iteration index, stop
condition inputs) is persisted alongside the runs it manages.

## Considered Options

In-run dynamic subgraph expansion was rejected. It would make `workflow.normalized.json` and
the definition checksum mutable during execution, break the inspect-the-graph-before-running
product promise, and complicate validation and resume — the exact properties ADR 0001 chose
the acyclic IR to protect.

## Consequences

- The kernel only ever executes static, pre-validated DAGs; validation happens once per run.
- New requirements: a run group id and iteration index in run state, persisted controller
  state, cross-run aggregate reporting, and group-level resume.
- Dynamic fan-out width is resolved at materialization time, never mid-run. A pattern that
  needs runtime-determined width materializes the run after the width is known.
