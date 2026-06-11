# PRD: CLI Agentic Workflow Orchestrator

Status: Draft
Date: 2026-06-11
Related ADR: `docs/adr/0001-local-first-python-bash-workflow-kernel.md`

## Summary

Build a lightweight local CLI workflow orchestrator for agentic coding and automation tasks.
The product uses existing agent CLI automation entrypoints, such as `claude -p` and
`codex exec`, as executable nodes in a programmable workflow graph.

The core product value is not another chat UI. It is a transparent workflow kernel that can
compose agent runs into repeatable patterns: pipelines, parallel branches, classify-and-act
flows, adversarial verification, generate-and-filter, fan-out synthesis, tournaments, and
loop-until-done iteration.

## Problem

Modern agent CLIs can perform useful autonomous work, but multi-step workflows are often
embedded in ad hoc prompts, shell scripts, or manually supervised sessions. This creates
several problems:

- Workflow intent is hard to inspect before execution.
- Intermediate outputs are not consistently persisted.
- Parallelism and retries are hand-written every time.
- Agent decisions are difficult to audit.
- Reusing a successful workflow pattern across repositories is cumbersome.
- Competing agent CLIs expose different automation surfaces.

Claude Code dynamic workflows show the product direction: users want larger tasks decomposed
and coordinated across many agents. This project targets a smaller, portable, local-first
version of that idea: explicit workflow definitions over existing CLI agents.

External capability notes retrieved from current documentation on 2026-06-11:

- Claude Code documents `/workflows` for dynamic workflows that orchestrate work across many
  background agents.
- Claude Code custom commands, hooks, plugins, and scripts already support explicit workflow
  fragments.
- Codex CLI documents non-interactive execution through `codex exec`, plus sandbox,
  approval, resume, app-server, and structured-output related capabilities.

## Goals

- Provide a local CLI for defining, validating, running, resuming, and reporting workflow runs.
- Define a minimal Workflow IR with nodes, edges, state, pipeline, parallel, and await
  semantics.
- Keep each concrete workflow run as a DAG for validation and recovery.
- Express iterative behavior through higher-level patterns that repeatedly instantiate or
  resume DAG runs.
- Support agent adapters for `claude -p` and `codex exec`.
- Support declarative workflow configuration files and formatted output.
- Provide built-in reusable patterns for common agentic workflows.
- Persist run state, logs, node outputs, and artifacts locally.
- Make failures inspectable and recoverable.

## Non-goals

- Replace Claude Code, Codex, or any agent CLI.
- Build a hosted control plane in v0.1.
- Build a distributed scheduler in v0.1.
- Require an external engine such as `iii engine` for v0.1.
- Build a browser UI in v0.1.
- Guarantee deterministic agent outputs.
- Hide security, sandbox, cost, or approval decisions behind opaque defaults.

## Feasibility Analysis

### Functional Feasibility

The project is feasible with Python and bash for v0.1.

Python can own the workflow kernel:

- Parse workflow definitions.
- Validate DAG structure.
- Build the Workflow IR.
- Execute nodes with dependency scheduling.
- Manage local concurrency.
- Persist state with `sqlite3`, JSONL logs, and artifact directories.
- Implement retries, timeouts, cancellation, and resume.
- Normalize adapter results.
- Render final reports.

Bash is useful as adapter glue:

- Wrap CLI commands.
- Set environment variables.
- Provide small compatibility scripts.
- Let users invoke project-local tools without requiring Python plugin code.

Pure bash is not sufficient for the core because graph validation, concurrency, structured
state, retries, artifact indexing, and testability become fragile quickly. Python should be
the orchestrator. Bash should be a leaf-level integration mechanism.

### Product Feasibility

The product is useful if it avoids becoming either a thin shell-script wrapper or a heavy
workflow platform.

The usable center is:

- `caw validate workflow.yaml` to catch mistakes before spending agent tokens.
- `caw graph workflow.yaml` to inspect the execution plan.
- `caw run workflow.yaml --input task.md` to execute.
- `caw resume <run-id>` to continue failed or interrupted runs.
- `caw report <run-id> --format markdown` to produce a durable result.
- `caw pattern init adversarial-verification` to scaffold known workflow shapes.

The strongest product advantage is explicitness: a user can see the graph, inputs, prompts,
outputs, retries, and final synthesis instead of relying on an invisible agent session.

### Usability and Product Experience

The product should feel like a small developer tool, not a platform that requires operational
setup before the first useful run.

The main usability requirements are:

- The first sample workflow should run locally without requiring a hosted service.
- Validation should fail before any agent tokens are spent.
- Error messages should name the workflow file, node id, adapter, and failed contract.
- `run`, `resume`, and `report` should be the primary happy path.
- Users should be able to inspect the normalized graph before execution.
- Built-in patterns should scaffold complete examples, not abstract templates.
- Agent CLI dependencies should be detected with actionable setup errors.
- Reports should make it clear which outputs are final conclusions and which are trace
  evidence.
- Defaults should be conservative for concurrency, retries, and destructive commands.
- Advanced users should be able to drop down to shell and Python nodes without writing a
  custom engine plugin.

### Competitive Position

Against Claude Code dynamic workflows:

- Weaker: not natively integrated into Claude Code, no first-party background agent fleet,
  fewer built-in UI affordances, and more adapter fragility.
- Stronger: vendor-neutral, inspectable, config-as-code, local-first, source-controlled,
  portable across agent CLIs, and easier to adapt to repo-specific conventions.

Against general workflow engines such as Airflow, Dagster, Prefect, or Temporal:

- Weaker: less mature scheduling, observability, scale, and distributed durability.
- Stronger: much lighter, agent-specific semantics, easier local setup, better fit for
  prompt/output/report workflows, and no service dependency.

Against ad hoc shell scripts:

- Weaker: more structure to learn.
- Stronger: validation, resume, reports, patterns, state, graph semantics, and reusable
  adapters.

## Missing Aspects to Include

The initial concept should explicitly cover:

- Run state durability and resume.
- Cancellation and timeout behavior.
- Retry policy and idempotency expectations.
- Artifact storage and cleanup.
- Structured output contracts and schema validation.
- Prompt template versioning.
- Sandbox and approval policy passthrough for agent CLIs.
- Secrets handling and environment variable policy.
- Token, cost, and rate-limit controls.
- Human approval gates for high-impact steps.
- Dry-run and graph inspection modes.
- Trace logs and machine-readable event streams.
- Adapter capability discovery.
- Workflow test fixtures and simulation mode.
- Compatibility boundaries for different agent CLI versions.

## Primary Personas

- Solo developer automating repetitive agentic coding workflows.
- Maintainer who wants repeatable review, triage, verification, or release workflows.
- Agent workflow designer who wants to package reusable patterns.
- Tooling engineer who wants a local, inspectable orchestrator before adopting heavier
  infrastructure.

## User Stories

- As a developer, I can define a workflow in a file and run it from the CLI.
- As a maintainer, I can inspect the planned graph before execution.
- As a maintainer, I can resume an interrupted workflow without repeating completed nodes.
- As a workflow author, I can compose primitive nodes into reusable patterns.
- As a reviewer, I can inspect every node input, output, failure, and artifact.
- As a user, I can choose whether a node runs through `claude -p`, `codex exec`, shell, or a
  local Python function.
- As a user, I can get final output in Markdown, JSON, JSONL, or plain text.

## Core Concepts

### Workflow IR

The Workflow IR is the validated internal model used by the executor. It should include:

- Workflow metadata.
- Inputs and variables.
- Node definitions.
- Edge definitions.
- Output contracts.
- Concurrency limits.
- Retry and timeout policies.
- State and artifact paths.

### Node

Required node fields:

- `id`
- `kind`
- `uses`
- `inputs`

Common optional fields:

- `needs`
- `when`
- `timeout`
- `retries`
- `output_schema`
- `env`
- `cwd`
- `artifacts`
- `approval`

Example node kinds:

- `agent`
- `shell`
- `python`
- `classify`
- `verify`
- `synthesize`
- `report`
- `human_gate`

### Edge

Edges represent:

- Ordering dependencies.
- Data dependencies.
- Conditional routing.

The v0.1 executor should reject cycles in the concrete run graph.

### State

State should record:

- Run id.
- Workflow definition checksum.
- Node status.
- Attempt count.
- Started and finished timestamps.
- Exit status.
- Normalized outputs.
- Artifact paths.
- Error classification.
- Resume eligibility.

### Pipeline

`pipeline` is syntactic sugar for a linear DAG.

### Parallel

`parallel` is syntactic sugar for independent branches that share the same parent dependency
and join into a downstream node.

### Await

`await` is a synchronization primitive. In v0.1 it should support:

- Waiting for dependency completion.
- Joining parallel branches.
- Waiting for an explicit human approval gate.

## Built-in Workflow Patterns

### Loop Until Done

Runs a DAG iteration, evaluates a done condition, and repeats until done, failed, or max
iterations is reached.

This is not a cyclic graph in the core IR. It is an iteration controller over repeated DAG
runs or repeated DAG subgraphs.

### Classify and Act

Runs a classifier node, maps the classification to one of several branches, then executes
the selected branch.

### Adversarial Verification

Runs a generator, runs one or more verifier nodes against the result, then either accepts,
rejects, or sends feedback into another iteration.

### Generate and Filter

Runs multiple candidate generators, filters candidates with a scoring or validation node,
then emits accepted candidates.

### Fan-out Synthesis

Runs multiple independent agents or prompts in parallel, then synthesizes a final answer.

### Tournament

Runs candidates in brackets or rounds, compares outputs, promotes winners, and produces a
final result with comparison evidence.

## CLI Requirements

Initial commands:

```text
caw init
caw validate <workflow-file>
caw graph <workflow-file>
caw run <workflow-file> [--input <file>] [--format <format>]
caw resume <run-id>
caw report <run-id> [--format json|jsonl|markdown|text]
caw patterns list
caw patterns init <pattern-name>
```

## Configuration Requirements

The preferred v0.1 format should be YAML because workflow definitions need readable nested
structures. TOML or JSON can be supported later, or used as a fallback if a dependency-free
stdlib-only mode is required.

Example sketch:

```yaml
name: review-and-fix
version: 1

inputs:
  task:
    type: file

nodes:
  - id: diagnose
    kind: agent
    uses: codex.exec
    inputs:
      prompt: "Diagnose the failure described in ${inputs.task}"
    output_schema: schemas/diagnosis.json

  - id: verify
    kind: agent
    uses: claude.print
    needs: [diagnose]
    inputs:
      prompt: "Review the diagnosis and identify gaps."

  - id: report
    kind: report
    needs: [diagnose, verify]
    inputs:
      format: markdown
```

## Architecture

Proposed package layout:

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
  reporters/
  adapters/
    base.py
    shell.py
    claude.py
    codex.py
  patterns/
    loop_until_done.py
    classify_and_act.py
    adversarial_verification.py
    generate_and_filter.py
    fan_out_synthesis.py
    tournament.py
tests/
```

Execution flow:

1. Parse workflow config.
2. Normalize into Workflow IR.
3. Validate schema, references, adapter names, and acyclic graph structure.
4. Plan executable node order and concurrency groups.
5. Create or resume local run state.
6. Execute ready nodes through adapters.
7. Persist events, outputs, artifacts, and errors.
8. Evaluate conditions and joins.
9. Render final output and run report.

## State and Artifacts

Suggested local run layout:

```text
.caw/
  runs/
    <run-id>/
      state.sqlite
      events.jsonl
      workflow.normalized.json
      artifacts/
        <node-id>/
          stdout.txt
          stderr.txt
          output.json
```

## Implementation Plan

### Phase 0: Product and Architecture Baseline

- Create PRD, ADR, and domain context.
- Define v0.1 scope and non-goals.
- Decide config format and CLI command name.

Exit criteria:

- PRD and ADR reviewed.
- Core terminology stable enough for issue creation.

### Phase 1: Project Scaffold

- Add Python package scaffold managed by `uv`.
- Add CLI entrypoint.
- Add lint, type check, and test commands.
- Add minimal CI when repository automation is introduced.

Exit criteria:

- `caw --help` runs locally.
- Basic tests run with `uv run`.

### Phase 2: Workflow IR and Validation

- Implement config parser.
- Implement typed Workflow IR.
- Implement graph validation.
- Implement dry-run graph rendering as text and JSON.

Exit criteria:

- Invalid workflow files fail before execution.
- A sample pipeline validates and renders.

### Phase 3: Local Executor and State

- Implement DAG scheduler.
- Implement state store.
- Implement event stream.
- Implement retries, timeouts, cancellation, and resume.

Exit criteria:

- A shell-only workflow can run, fail, resume, and report.

### Phase 4: Agent Adapters

- Implement `codex.exec` adapter.
- Implement `claude.print` adapter for `claude -p`.
- Normalize exit codes, stdout, stderr, structured outputs, and artifacts.
- Add adapter capability checks.

Exit criteria:

- Sample workflows can call both adapters when the external CLIs are installed.
- Missing CLI dependencies produce clear errors.

### Phase 5: Built-in Patterns

- Implement pattern expanders for pipeline, parallel, classify-and-act, fan-out synthesis,
  generate-and-filter, adversarial verification, tournament, and loop-until-done.

Exit criteria:

- Each pattern has at least one example workflow and test coverage.

### Phase 6: Reporting and Hardening

- Add Markdown, JSON, JSONL, and text reporters.
- Add schema validation for final outputs.
- Add artifact cleanup policy.
- Add human approval gate support if not implemented earlier.

Exit criteria:

- A real repository workflow can produce a reviewable report with trace evidence.

## Success Metrics

- A new user can run a sample workflow within 10 minutes.
- Invalid workflows fail before any agent CLI invocation.
- Interrupted shell-only workflows can resume without repeating completed nodes.
- Final reports include the graph, node statuses, artifacts, and errors.
- Built-in patterns reduce repeated workflow boilerplate by at least 50 percent compared with
  handwritten scripts.

## Key Risks

- External Agent CLI output formats and flags may change.
- Agent outputs are nondeterministic and may not match schemas.
- Parallel agent runs can consume tokens quickly.
- Long-running local workflows can be interrupted by machine sleep, terminal shutdown, or
  auth expiration.
- Too many primitives can make the product feel like a general workflow engine instead of an
  agent-specific tool.
- Too little structure can make it indistinguishable from shell scripts.

## Requirement Traceability

| Objective requirement | Covered by |
| --- | --- |
| Feasibility analysis | `Feasibility Analysis`, `Key Risks` |
| Product usability and competitiveness | `Usability and Product Experience`, `Competitive Position` |
| Missing product aspects | `Missing Aspects to Include`, `Open Questions` |
| Python and bash feasibility | `Functional Feasibility` |
| Need for `iii engine` or similar infrastructure | PRD `Non-goals`, ADR decision and alternatives |
| Architecture design | `Architecture`, `State and Artifacts`, related ADR |
| Implementation flow | `Implementation Plan` |
| Primitive workflow model | `Core Concepts` |
| Built-in workflow patterns | `Built-in Workflow Patterns` |
| Config-defined workflows and formatted output | `Configuration Requirements`, `CLI Requirements`, `Reporting and Hardening` |

## Open Questions

1. Should the CLI command be `caw`, `agentflow`, or another name?
2. Should YAML be the default config format despite adding a dependency?
3. Should human approval gates be required in v0.1?
4. What exactly is meant by `iii engine`, and is it a concrete dependency candidate or a
   shorthand for durable workflow infrastructure?
5. Which workflow pattern should be the first end-to-end sample?

## References

External references were checked on 2026-06-11 through Context7.

- Claude Code changelog, including dynamic workflow notes:
  https://github.com/anthropics/claude-code/blob/main/claude-code/CHANGELOG.md
- Claude Code command development examples:
  https://github.com/anthropics/claude-code/blob/main/plugins/plugin-dev/skills/command-development/SKILL.md
- Claude Code plugin feature reference:
  https://github.com/anthropics/claude-code/blob/main/plugins/plugin-dev/skills/command-development/references/plugin-features-reference.md
- Codex CLI command definitions:
  https://github.com/openai/codex/blob/main/codex-rs/cli/src/main.rs
- Codex app-server automation API examples:
  https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md
