# ADR 0001: Use a Local-first Python Workflow Kernel with Bash Adapters

Status: Accepted
Date: 2026-06-11
Related: `docs/prd/0001-cli-agentic-workflow.md`,
`docs/adr/0002-pattern-iteration-as-run-groups.md`

The project needs a v0.1 runtime for composing agent CLI entrypoints such as `claude -p`
and `codex exec` into inspectable, repeatable workflows. We build a local-first workflow
kernel in Python — orchestrator, scheduler, state manager, validator, and reporter — with
bash only as leaf-level adapter glue and no mandatory external workflow framework. The
riskiest v0.1 unknowns are product and workflow semantics, not distributed execution;
Python covers subprocess control, timeouts, concurrency, and SQLite persistence, so an
external engine would add deployment cost and lock in execution assumptions before the
core model stabilizes.

Concretely: agent CLIs are adapter targets, never embedded runtime dependencies; run
state persists locally as SQLite plus JSONL events, a normalized workflow snapshot, and
artifact directories; each concrete Workflow IR stays acyclic, with iteration modeled as
run groups (ADR 0002); and the kernel fronts an internal execution-backend interface so a
future durable engine can be introduced without rewriting workflow definitions.
Implementation specifics — package layout, run-directory layout, state fields, execution
flow — live in the PRD.

## Considered Options

- **Pure bash** — rejected for the core runtime: graph validation, structured state,
  retries, concurrency, artifact indexing, and resume become fragile; pure bash would
  recreate a workflow engine poorly.
- **Python only, no bash** — rejected as too restrictive: bash wrappers stay valuable for
  integrating existing local commands, environment setup, and preserving the CLI-native
  character of the project.
- **External workflow framework in v0.1**, such as `iii`
  (https://github.com/iii-hq/iii) — postponed: useful later for distributed execution,
  worker queues, remote runs, or long-lived background workflows, but none of those is a
  v0.1 requirement. Reconsider when at least one becomes mandatory: distributed worker
  execution, durable queues across process and machine restarts, a multi-user control
  plane, remote run management, fan-out across dozens to hundreds of concurrent agents,
  or centralized observability and scheduling.
- **General workflow engine** (Airflow, Dagster, Prefect, Temporal) — postponed: mature
  but heavier than the desired v0.1 product, and none directly models agent-specific
  concerns such as prompt templates, structured model outputs, human approval gates,
  adversarial verification, or token-aware fan-out.

## Consequences

- Simple local installation, inspectable on-disk state, source-controlled workflows, and
  a vendor-neutral adapter model with a clean path to supporting multiple agent CLIs.
- No distributed execution in v0.1: long-running workflows are bounded by local machine
  availability, and concurrency by CPU, memory, auth, and API rate limits.
- Adapter behavior may break when external CLI flags or output formats change.
- The project must define its own recovery semantics instead of delegating them to a
  mature durable workflow engine.
