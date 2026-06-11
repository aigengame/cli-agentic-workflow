# ADR 0004: v0.1 Python Stack and Toolchain

Status: Accepted
Date: 2026-06-11
Related: `docs/adr/0001-local-first-python-bash-workflow-kernel.md`,
`docs/adr/0003-asyncio-executor-concurrency-model.md`,
`docs/prd/0001-cli-agentic-workflow.md`

A consolidated record of stack choices that individually sit below the one-ADR-per-decision
bar (each is conventional and reversible), recorded together so implementers find the
rationale here instead of re-litigating them per issue.

- **pydantic v2** models the typed Workflow IR, validates workflow config shape, and
  serializes the normalized snapshot. Its structured validation errors carry field paths
  that feed the "name the workflow file, node id, and failed contract" error requirement.
  Graph-level validation (cycles, references) remains custom validator logic. Plain
  dataclasses were rejected: hand-written nested validation would undercut the
  validate-before-spending-tokens product promise.
- **JSON Schema draft 2020-12**, validated with the `jsonschema` library, is the Output
  Contract dialect. Contracts are user assets referenced as files (`output_schema: <path>`)
  in v0.1; inline schemas are a later extension. JSON Schema is the agent-CLI lingua
  franca, so adapters can pass the same contract down to the underlying CLI's
  structured-output feature where supported. Pydantic models were rejected as the contract
  format: they would force workflow authors to write Python and break adapter symmetry.
- **typer** provides the CLI layer: nested command groups (`caw patterns ...`), shell
  completion, and formatted help with minimal boilerplate. Type-hint-driven parsing is
  consistent with the pydantic choice. Commands are synchronous entrypoints that enter the
  executor via `asyncio.run`.
- **Python >= 3.12** is the minimum supported version; CI tests 3.12, 3.13, and 3.14.
  Everything the kernel needs (`asyncio.TaskGroup`, `asyncio.timeout`, `except*`) exists by
  3.12; 3.11 approaches end of life, and 3.12 is the current LTS-distro baseline.
- **ruff** (lint and format), **mypy --strict** with the pydantic plugin, and **pytest**
  with **pytest-asyncio** form the toolchain, all run through `uv`. The type checker is
  deliberately swappable and carries no lock-in.
