# ADR 0008: Pattern Expanders Compile to Plain IR at Normalize Time via a Registry

Status: Accepted
Date: 2026-06-15
Related: `docs/adr/0001-local-first-python-bash-workflow-kernel.md`,
`docs/adr/0002-pattern-iteration-as-run-groups.md`, `CONTEXT.md`, issue #8

CONTEXT.md defines a **Pattern Expander** as a pattern realization that compiles into a
static subgraph inside a single Run at materialization time — "Expanders shape one Run's
graph; Pattern Controllers sequence multiple Runs." This records how the first expanders
(`pipeline`, `parallel`) are shaped, where expansion happens, and why this is a separate
axis from ADR 0002's controller/run-group iteration.

A Pattern Expander compiles to **plain Workflow IR at normalize time**, before validation.
The authoring surface is a top-level `pattern:` block in the workflow YAML, **mutually
exclusive with `nodes:`**: a file declares EITHER `pattern:` (the expander materializes the
nodes) OR `nodes:` (hand-authored), never both — enforced as a config error. The expanders
live in a **registry** mapping `name -> expander`; each expander declares its own pydantic
params model and an `expand` function returning plain node dicts. `normalize_workflow` runs
`expand_pattern` BEFORE `Workflow.model_validate`: it enforces the `pattern:`-XOR-`nodes:`
rule, looks the expander up by `pattern.type`, validates its params (surfacing failures
through the existing one-line `WorkflowConfigError` contract with a field path), and
replaces `pattern:` with the expanded `nodes:`.

The product of expansion is an **ordinary `Workflow`**. Acyclic validation,
`definition_checksum`, the persisted run snapshot, `caw graph`, resume, and `execute_run`
all operate on it **unchanged** — no special-casing anywhere downstream. An expanded
workflow is therefore IDENTICAL to its hand-authored `nodes:` equivalent: the same
normalized snapshot and the same checksum (asserted directly in the tests). The two v0.1
expanders: `pipeline` chains ordered steps into a linear `needs` chain; `parallel` emits
independent branches plus an optional downstream join node that `needs` every branch and
may carry its own `join` policy (ADR 0007). Step/branch/join entries carry the same node
fields a hand-authored node has; the expander owns only the injected `needs`, so a step or
branch declaring its own `needs` is rejected.

Registering a new expander is **additive** — `register_expander(name, params_model, expand,
shape)` adds a registry entry with no edit to a dispatch elsewhere — so issue #13's three
further expanders register beside these two. `caw patterns list` is driven off the registry
(new patterns appear automatically), and `caw patterns init <name>` scaffolds a complete,
runnable example bundle for each.

## Considered Options

- **In-run dynamic subgraph expansion (mutate the graph mid-Run)** — rejected, exactly as
  ADR 0002 rejected it: it would make the snapshot and checksum mutable during execution
  and break the inspect-the-graph-before-running promise. Expanders run at normalize time,
  producing a static pre-validated DAG, so that promise holds.
- **A `match`/`if` dispatch over a `pattern.type` enum** — rejected: every new expander
  would edit a central dispatch and a closed `Literal`. A registry keyed by name makes
  registration additive and keeps `caw patterns list` truthful with zero coupling.
- **A new IR node type for patterns the executor interprets** — rejected: it would push
  pattern semantics into the kernel and the executor, duplicating scheduling logic. Plain
  IR keeps the kernel ignorant of patterns; an expander is pure compile-time sugar.
- **Reusing ADR 0002's Pattern Controller for `pipeline`/`parallel`** — rejected: those
  shapes are a single Run's static graph, not a sequence of Runs. Controllers (run groups,
  iteration index, stop conditions) are a heavier, distinct axis; `pipeline`/`parallel` need
  only graph shaping, which is this ADR's expander axis.

## Consequences

- Pattern sugar adds no kernel, executor, validation, checksum, or resume surface: it is a
  pre-validation source-to-source transform into the existing IR.
- The registry is the single extension point; #13 (classify-and-act, generate-and-filter,
  fan-out-synthesis) and later expanders are additive registrations plus scaffold examples.
- `pattern:` and `nodes:` are mutually exclusive by construction, so there is one
  authoritative node source per file and no ambiguity about what the kernel runs.
- This ADR is orthogonal to ADR 0002: an expander shapes one Run's graph; a Pattern
  Controller sequences multiple Runs in a Run Group. A future pattern can use both.
