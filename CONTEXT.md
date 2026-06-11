# caw

caw is a lightweight, local-first workflow kernel and CLI that composes external agent CLI
entrypoints, such as `claude -p` and `codex exec`, into explicit, inspectable, repeatable
workflow runs. It is not an agent model provider; agent CLIs stay external and are
integrated through Adapters.

## Language

### CLI and Integration

**caw**:
This project's workflow CLI — the command users run to validate, execute, resume, and
report workflows.

**Agent CLI**:
An external command-line agent runner, such as `claude -p` or `codex exec`.
_Avoid_: agent runner, model provider

**Adapter**:
The project-owned integration layer that invokes an Agent CLI and normalizes its result
into the workflow runtime.
_Avoid_: connector, wrapper, plugin

**Reporter**:
A component that formats traces, intermediate artifacts, and final output as JSON,
Markdown, text, or other target formats.
_Avoid_: formatter, printer

**Engine Backend**:
The execution substrate behind the workflow kernel; the v0.1 backend is the local Python
process.
_Avoid_: engine, runtime

### Workflow Model

**Workflow**:
An executable graph of nodes and dependencies that transforms input state into artifacts,
decisions, or final output.
_Avoid_: pipeline, DAG, graph

**Workflow IR**:
The internal representation of a workflow after parsing and validation. Each concrete
run's IR is acyclic and immutable once execution starts (see ADR 0002).
_Avoid_: AST

**Node**:
A unit of work in a workflow, such as invoking an agent, running a shell command,
transforming or classifying data, verifying or synthesizing results, reporting artifacts,
or pausing for human approval.
_Avoid_: step, task, stage

**Edge**:
A dependency between nodes expressing ordering and data flow only; conditional behavior
lives in a Node's `when` predicate, never on an Edge.
_Avoid_: transition, link

**Output Contract**:
The declared schema that a Node's normalized output must satisfy, validated by the kernel
when the node completes and before dependents run.
_Avoid_: output schema, result type

### Run and State

**Run**:
One execution of a workflow definition with a specific input, configuration, and run id.
_Avoid_: execution, job

**Attempt**:
One execution attempt of a node within a run.
_Avoid_: retry, try

**State**:
Durable run data: inputs, node status, outputs, artifacts, attempts, errors, and metadata.
_Avoid_: session, run context

**Artifact**:
A durable file produced by a node attempt during a run, indexed in the run's State.
_Avoid_: output file, deliverable

**Event**:
An append-only record of one occurrence during a run; the event sequence is the
machine-readable trace of a run.
_Avoid_: log entry

### Patterns and Composition

**Pipeline**:
A linear composition of nodes.
_Avoid_: chain, sequence

**Parallel**:
A composition that runs independent branches concurrently and joins their results.
_Avoid_: fork-join

**Await**:
A primitive that parks a run on a condition outside the graph until the condition is
satisfied. Waiting for upstream nodes to complete is ordinary Edge scheduling, not an
Await.
_Avoid_: wait, sleep, poll

**Human Gate**:
The human-in-the-loop specialization of Await, where the external condition is an explicit
human approval to continue the run.
_Avoid_: approval step, manual step

**Pattern**:
A reusable higher-level workflow shape, such as loop-until-done, classify-and-act,
adversarial verification, generate-and-filter, fan-out-synthesis, or tournament; realized
by a Pattern Expander, a Pattern Controller, or both.
_Avoid_: template, recipe

**Pattern Expander**:
A pattern realization that compiles into a static subgraph inside a single Run at
materialization time. Expanders shape one Run's graph; Pattern Controllers sequence
multiple Runs.
_Avoid_: macro

**Pattern Controller**:
The pattern-level component that expresses iterative behavior by evaluating a finished Run
and materializing the next one; the kernel itself only executes acyclic Runs (see ADR
0002).
_Avoid_: orchestrator, loop controller

**Run Group**:
The set of Runs materialized by one Pattern Controller execution; the unit of aggregate
reporting and resumption for iterative patterns.
_Avoid_: batch
