# caw

caw is a lightweight, local-first CLI that orchestrates AI agent CLIs like `claude -p` and
`codex exec` into powerful, inspectable workflows — define a DAG in simple YAML, then
validate, run, resume, and report with zero infrastructure.

caw is not another chat UI and not an agent model provider. It is a local workflow kernel
that turns agent invocations into structured, repeatable workflow runs: every graph is
visible before execution, every node output is persisted, and every run can be resumed and
audited.

## Project status

**Pre-release — v0.1 specification complete, implementation in progress.**

The product scope, architecture, and vocabulary are fully specified and frozen in
[PRD #1](https://github.com/aigengame/cli-agentic-workflow/issues/1), with implementation
broken into tracer-bullet issues ([#2–#17](https://github.com/aigengame/cli-agentic-workflow/issues)).
The commands and examples below describe the specified v0.1 surface; they become runnable
as those issues land.

## Why caw

- **Validate before you spend tokens.** `caw validate` catches schema errors, broken
  references, and dependency cycles before any agent CLI is invoked.
- **See the graph before it runs.** `caw graph` renders the execution plan; the normalized
  workflow snapshot is immutable once a run starts.
- **Vendor-neutral by design.** `claude -p` and `codex exec` are adapters with symmetric
  capabilities — switch an agent node between them by changing one `uses` value.
- **Resume instead of re-run.** Run state, events, and artifacts persist locally
  (SQLite + JSONL); interrupted runs continue without repeating completed nodes.
- **Human gates for high-impact steps.** A `human_gate` node parks the run durably until
  you approve — interactively or via `caw resume --approve`.
- **Reusable agentic patterns.** Pipeline, parallel, classify-and-act, generate-and-filter,
  fan-out synthesis, adversarial verification, tournament, and loop-until-done ship as
  built-ins that scaffold complete, runnable examples.
- **Reports you can hand to a reviewer.** Markdown, JSON, JSONL, or plain-text reports
  separate final conclusions from trace evidence.
- **Local-first, zero infrastructure.** One machine, one process, inspectable files on
  disk. No server, no control plane, no external workflow engine.

## How it works

A workflow is a YAML file describing nodes (agent calls, shell commands, Python functions,
classifiers, verifiers, synthesizers, reports, human gates) and the edges between them.
caw normalizes it into an acyclic, immutable intermediate representation, schedules ready
nodes concurrently on an asyncio event loop, and persists everything under `.caw/runs/<run-id>/`:

```text
.caw/runs/<run-id>/
  state.sqlite                # node status, attempts, outputs, resume eligibility
  events.jsonl                # append-only machine-readable trace
  workflow.normalized.json    # the exact graph that ran, with checksum
  artifacts/<node-id>/        # stdout, stderr, structured outputs
```

Iterative behavior (loops, regeneration, tournament rounds) never mutates a running graph:
a pattern controller evaluates a finished run and materializes the next immutable run,
linking them into a run group that reports and resumes as a unit.

Conditional behavior lives in node-level `when` predicates; structured outputs are
validated against JSON Schema (draft 2020-12) output contracts; env vars reach a node only
when explicitly declared and are never persisted.

## Example

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

```bash
caw validate review-and-fix.yaml   # fail fast, before tokens
caw graph review-and-fix.yaml      # inspect the plan
caw run review-and-fix.yaml --input task.md
caw report <run-id> --format markdown
```

## CLI at a glance

| Command | Purpose |
| --- | --- |
| `caw init` | Create a minimal starter workflow |
| `caw validate <file>` | Check schema, references, adapters, and acyclicity without executing |
| `caw graph <file>` | Render the planned DAG as text or JSON |
| `caw run <file>` | Execute a workflow run |
| `caw resume <run-id>` | Continue an interrupted, failed, or parked run |
| `caw report <run-id>` | Render a report (markdown, json, jsonl, text) from persisted state |
| `caw patterns list` | List built-in workflow patterns |
| `caw patterns init <name>` | Scaffold a complete runnable example of a pattern |

## Built-in patterns

| Pattern | Shape |
| --- | --- |
| Pipeline | Linear node chain |
| Parallel | Independent branches joined downstream |
| Classify and act | Classifier routes to one of several `when`-gated branches |
| Generate and filter | N candidate generators, then a scoring/validation filter |
| Fan-out synthesis | Parallel agents, then a synthesis node (the reference sample runs `claude.print` and `codex.exec` side by side) |
| Adversarial verification | Generator + verifiers, with accept / reject / regenerate |
| Tournament | Rounds or brackets with winner promotion and comparison evidence |
| Loop until done | Iterates immutable runs in a run group until a stop condition |

## Positioning

- **vs. Claude Code dynamic workflows** — caw is not natively integrated and has no
  background agent fleet, but it is vendor-neutral, config-as-code, source-controlled, and
  portable across agent CLIs.
- **vs. Airflow / Dagster / Prefect / Temporal** — caw has none of their distributed
  durability, and deliberately so: it is far lighter, models agent-specific concerns
  (prompts, output contracts, approval gates, token usage), and needs no service.
- **vs. ad hoc shell scripts** — more structure to learn, in exchange for validation,
  resume, state, reports, and reusable patterns.

## Documentation

- Product spec: [`docs/prd/0001-cli-agentic-workflow.md`](docs/prd/0001-cli-agentic-workflow.md)
- Architecture decisions: [`docs/adr/`](docs/adr/) — local-first kernel (0001), run-group
  iteration (0002), asyncio executor (0003), Python stack (0004)
- Domain vocabulary: [`CONTEXT.md`](CONTEXT.md)

## Development

Python >= 3.12, managed with [uv](https://docs.astral.sh/uv/):

```bash
uv sync          # install
uv run pytest    # tests
uv run ruff check && uv run ruff format --check
uv run mypy
```

Tests exercise external behavior only, through three seams: the CLI itself, a
fixture-replaying mock adapter (no tokens, no real CLIs), and the on-disk run directory.
Real-CLI adapter tests skip automatically when `claude` or `codex` is not installed.

## Contributing

Work is tracked as GitHub issues with a triage-label workflow; issues labeled
`ready-for-agent` are fully specified and independently grabbable. Start from
[PRD #1](https://github.com/aigengame/cli-agentic-workflow/issues/1) for the big picture.
Commits follow [Conventional Commits](https://www.conventionalcommits.org/).

## License

[MIT](LICENSE)
