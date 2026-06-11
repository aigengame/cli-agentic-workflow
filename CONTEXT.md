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
- A mandatory external engine such as `iii engine`.

## Ubiquitous Language

- **Agent CLI**: An external command-line agent runner, such as `claude -p` or `codex exec`.
- **Adapter**: The project-owned integration layer that invokes an Agent CLI and normalizes
  its result into the workflow runtime.
- **Workflow**: An executable graph of nodes and dependencies that transforms input state
  into artifacts, decisions, or final output.
- **Workflow IR**: The internal representation of a workflow after parsing and validation.
- **Node**: A unit of work. A node can invoke an agent, run a shell command, transform data,
  classify output, verify results, or report artifacts.
- **Edge**: A dependency between nodes. Edges can represent ordering, data flow, or conditional
  routing.
- **State**: Durable run data, including inputs, node status, outputs, artifacts, attempts,
  errors, and metadata.
- **Run**: One execution of a workflow definition with a specific input, configuration, and
  run id.
- **Attempt**: One execution attempt of a node within a run.
- **Pipeline**: A linear composition of nodes.
- **Parallel**: A composition that runs independent branches concurrently and joins their
  results.
- **Await**: A synchronization point that waits for dependencies, external results, or a
  human approval gate.
- **Pattern**: A reusable higher-level workflow shape, such as `loop until done`,
  `classify-and-act`, `adversarial verification`, `generate-and-filter`,
  `fan-out-synthesis`, or `tournament`.
- **Reporter**: A component that formats traces, intermediate artifacts, and final output
  as JSON, Markdown, text, or other target formats.
- **Engine Backend**: The execution substrate behind the workflow kernel. The v0.1 backend is
  the local Python process. Future backends may use external durable engines.

## Important Modeling Constraint

The core Workflow IR should remain acyclic for each concrete run. Cyclic behavior such as
`loop until done` should be modeled as a pattern-level iteration that repeatedly materializes
or resumes acyclic graph runs until a stopping condition is satisfied.

This keeps validation, state recovery, and execution semantics simple while preserving the
ability to express higher-level iterative workflows.

## Open Product Decisions

- Whether the default declarative workflow format should be YAML, TOML, or JSON.
- Which Agent CLI adapters are required for the first release.
- Whether human approval gates are mandatory in v0.1 or a later hardening feature.
- Whether `iii engine` refers to a specific existing engine or a placeholder for any durable
  workflow infrastructure.
