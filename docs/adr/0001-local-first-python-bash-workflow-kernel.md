# ADR 0001: Use a Local-first Python Workflow Kernel with Bash Adapters

Status: Accepted
Date: 2026-06-11
Related PRD: `docs/prd/0001-cli-agentic-workflow.md`

## Context

The project aims to build a lightweight command-line agentic workflow orchestrator. It should
compose agent automation entrypoints such as `claude -p` and `codex exec` into programmable
workflows with primitives including node, edge, state, pipeline, parallel, and await.

The product is intended to benchmark the direction shown by Claude Code dynamic workflows,
but with a local-first, inspectable, vendor-neutral implementation. Current external docs show
that Claude Code exposes dynamic workflows through `/workflows`, while Claude Code custom
commands, hooks, and plugin scripts support explicit workflow fragments. Codex CLI exposes
non-interactive `exec` behavior and related automation surfaces.

The core architectural question is whether v0.1 should be implemented with Python and bash
alone, or whether it should immediately adopt a heavier engine such as `iii engine` or another
durable workflow backend.

## Decision

For v0.1, build a local-first workflow kernel in Python and use bash only for leaf-level
adapter glue.

The v0.1 architecture will:

- Use Python as the orchestrator, scheduler, state manager, validator, and reporter.
- Use bash scripts as optional wrappers around external tools and project-local commands.
- Treat `claude -p`, `codex exec`, and shell commands as adapter targets, not as embedded
  runtime dependencies.
- Persist run state locally using SQLite, JSONL events, normalized workflow snapshots, and
  artifact directories.
- Keep each concrete Workflow IR acyclic.
- Implement loops and tournaments as pattern-level controllers over repeated or expanded DAGs.
- Avoid requiring `iii engine` or any external durable workflow infrastructure in v0.1.
- Define an internal execution backend interface so a future engine can be introduced without
  rewriting workflow definitions.

## Rationale

Python is enough for the v0.1 requirements:

- Workflow parsing and validation.
- Typed internal models.
- DAG scheduling.
- Local parallelism.
- Subprocess execution.
- Timeout and retry control.
- State persistence through `sqlite3`.
- Structured logs and reports.
- Unit and integration testing.

Bash remains useful, but only at the edges. It is a good compatibility layer for CLI tools,
environment setup, and small wrappers. It is not a good primary runtime for graph validation,
resumable state, concurrency, and structured reporting.

An external engine is not justified at v0.1 because the riskiest unknowns are product and
workflow semantics, not distributed execution. Introducing an engine too early would add
deployment cost, lock in execution assumptions, and slow iteration on the core model.

## Consequences

Positive consequences:

- Simple local installation and development.
- Lower operational complexity.
- Clear inspectable state on disk.
- Easier source-controlled workflows.
- Easier testing of the workflow kernel.
- Vendor-neutral adapter model.
- A clean path to support multiple agent CLIs.

Negative consequences:

- No built-in distributed execution in v0.1.
- Local machine availability limits long-running workflows.
- Local concurrency is constrained by CPU, memory, auth, and API rate limits.
- Adapter behavior may break when external CLI flags or output formats change.
- The project must define its own recovery semantics instead of delegating them to a mature
  durable workflow engine.

## Alternatives Considered

### Pure Bash

Rejected for the core runtime.

Bash can launch commands, but complex graph validation, structured state, retries, concurrency,
artifact indexing, tests, and resume behavior become fragile. Pure bash would likely recreate a
workflow engine poorly.

### Python Only, No Bash

Rejected as too restrictive.

Python should own the runtime, but bash wrappers are valuable for integrating existing local
commands, setting up environments, and preserving the CLI-native nature of the project.

### Adopt `iii engine` or other extern engine/runtime in v0.1

Postponed.

If `iii engine` is a specific durable workflow engine, it may be useful later for distributed
execution, worker queues, remote runs, or long-lived background workflows. It should not be a
mandatory v0.1 dependency unless the product scope requires those capabilities immediately.

Adoption should be reconsidered when at least one of these requirements becomes mandatory:

- Distributed worker execution.
- Durable queues across process and machine restarts.
- Multi-user control plane.
- Remote run management.
- Large fan-out across dozens or hundreds of concurrent agents.
- Centralized observability and scheduling.

### Use a General Workflow Engine

Postponed.

Airflow, Dagster, Prefect, Temporal, and similar systems are mature, but they are heavier than
the desired v0.1 product. They also do not directly model agent-specific concerns such as prompt
templates, structured model outputs, human approval gates, adversarial verification, or
token-aware fan-out.

The project may later provide exporters or backend integrations if local execution becomes a
limitation.

## Architecture Sketch

```text
workflow config
  -> parser
  -> Workflow IR
  -> validator
  -> planner
  -> local executor backend
  -> adapter invocation
  -> state store and event log
  -> reporters
```

Suggested package layout:

```text
src/caw/
  cli.py
  config.py
  model.py
  validate.py
  planner.py
  executor.py
  scheduler.py
  state.py
  events.py
  artifacts.py
  adapters/
  patterns/
  reporters/
```

## State Model

Each run should create a local run directory:

```text
.caw/runs/<run-id>/
  state.sqlite
  events.jsonl
  workflow.normalized.json
  artifacts/
```

The state store should record:

- Workflow identity and definition checksum.
- Node status.
- Attempt history.
- Inputs and normalized outputs.
- Artifact paths.
- Errors and retry state.
- Resume eligibility.

## Workflow Semantics

The concrete Workflow IR is a DAG. That gives the kernel clear semantics for validation,
scheduling, dependency completion, and resume.

Higher-level patterns may express behavior that feels cyclic:

- `loop until done`
- adversarial verification with regeneration
- tournament rounds

These patterns should compile into repeated or expanded DAG executions rather than cycles in
the core IR.

## Future Reconsideration Triggers

Reopen this ADR if:

- Users need remote runs or distributed workers.
- Local state recovery proves insufficient.
- Workflows regularly run for hours or days.
- A built-in background run manager becomes a product requirement.
- The project needs to coordinate tens to hundreds of concurrent agents.
- `iii engine` is confirmed to provide essential capabilities that cannot be replicated
  locally at acceptable complexity.
