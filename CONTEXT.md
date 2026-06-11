# Context

## Project Nature

This repository defines a lightweight command-line agentic workflow orchestration project.
It composes existing agent CLI automation entrypoints, such as `claude -p` and `codex exec`,
with Python and shell glue to build explicit, inspectable, repeatable workflows.

The project is not an agent model provider. It is a local workflow kernel and CLI for
turning agent invocations into structured workflow runs.

## Current Product Boundary

The confirmed v0.1 boundary is a local-first, single-machine workflow runner:

- Run workflows from the command line.
- Treat agent CLIs as external adapters.
- Persist local run state and artifacts for inspection and resume.
- Support declarative workflow configuration and formatted output.
- Provide reusable workflow patterns built from lower-level primitives.

Out of scope for v0.1:

- Distributed scheduling.
- Multi-tenant control planes.
- Hosted web UI.
- Long-lived remote worker fleets.
- A self-hosted replacement for Claude Code dynamic workflows.
- A mandatory external workflow framework, such as `iii`.

## Ubiquitous Language

- **caw**: The name of this project's workflow CLI. The command users run to validate,
  execute, resume, and report workflows.
- **Agent CLI**: An external command-line agent runner, such as `claude -p` or `codex exec`.
- **Adapter**: The project-owned integration layer that invokes an Agent CLI and normalizes
  its result into the workflow runtime.
- **Workflow**: An executable graph of nodes and dependencies that transforms input state
  into artifacts, decisions, or final output.
- **Workflow IR**: The internal representation of a workflow after parsing and validation.
- **Node**: A unit of work. A node can invoke an agent, run a shell command, transform data,
  classify output, verify results, synthesize results from multiple inputs, report artifacts,
  or pause the run for human approval.
- **Edge**: A dependency between nodes. Edges represent ordering and data flow only;
  conditional behavior is expressed by a Node's `when` predicate, never on an Edge.
- **Output Contract**: The declared schema that a Node's normalized output must satisfy.
  Validated by the kernel when the node completes, before dependents run.
- **State**: Durable run data, including inputs, node status, outputs, artifacts, attempts,
  errors, and metadata.
- **Artifact**: A durable file produced by a node attempt during a run, indexed in the
  run's State.
- **Event**: An append-only record of one occurrence during a run, such as a node starting,
  an attempt failing, or the run parking. The event sequence is the machine-readable trace
  of a run.
- **Run**: One execution of a workflow definition with a specific input, configuration, and
  run id.
- **Attempt**: One execution attempt of a node within a run.
- **Pipeline**: A linear composition of nodes.
- **Parallel**: A composition that runs independent branches concurrently and joins their
  results.
- **Await**: A primitive that parks a run on a condition outside the graph. The awaiting
  node enters a waiting state, run state is persisted, and execution resumes when the
  condition is satisfied. Waiting for upstream nodes to complete is not an Await; that is
  ordinary Edge scheduling.
- **Human Gate**: The human-in-the-loop specialization of Await. The external condition is
  an explicit human approval to continue the run.
- **Pattern**: A reusable higher-level workflow shape, such as `loop until done`,
  `classify-and-act`, `adversarial verification`, `generate-and-filter`,
  `fan-out-synthesis`, or `tournament`. A pattern is realized by a Pattern Expander, a
  Pattern Controller, or both.
- **Pattern Expander**: A pattern realization that compiles into a static subgraph inside a
  single Run at materialization time, such as pipeline, parallel, or fan-out synthesis.
  Expanders shape one Run's graph; Pattern Controllers sequence multiple Runs.
- **Pattern Controller**: The pattern-level component that expresses iterative behavior. It
  evaluates a finished Run and materializes the next one; the kernel itself only executes
  acyclic Runs.
- **Run Group**: The set of Runs materialized by one Pattern Controller execution. The Run
  Group is the unit of aggregate reporting and resumption for iterative patterns.
- **Reporter**: A component that formats traces, intermediate artifacts, and final output
  as JSON, Markdown, text, or other target formats.
- **Engine Backend**: The execution substrate behind the workflow kernel. The v0.1 backend is
  the local Python process. Future backends may use external durable engines.

## Important Modeling Constraint

The core Workflow IR remains acyclic for each concrete run, and each materialized run graph
is immutable once execution starts. Cyclic behavior such as `loop until done` is modeled at
the pattern level: a Pattern Controller evaluates a finished Run and materializes the next
one, linking successive Runs into a Run Group, until a stopping condition is satisfied
(see `docs/adr/0002-pattern-iteration-as-run-groups.md`).

This keeps validation, state recovery, and execution semantics simple while preserving the
ability to express higher-level iterative workflows.