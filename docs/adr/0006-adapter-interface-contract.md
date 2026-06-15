# ADR 0006: The Adapter Interface Contract

Status: Accepted
Date: 2026-06-13
Related: `docs/adr/0001-local-first-python-bash-workflow-kernel.md`,
`docs/adr/0003-asyncio-executor-concurrency-model.md`,
`docs/adr/0004-python-stack-and-toolchain.md`, issues #5, #9, #11, #16, #66

ADR 0001 keeps Agent CLIs external and integrated through Adapters but left the Adapter
boundary unspecified. This records the contract, since #9 (claude), #11 (codex), and the
pattern issues build on it and a later change would ripple through all of them.

An **Adapter** is an abstract async interface with one method,
`invoke(AgentInvocation) -> AgentResult`, over two vendor-neutral data classes. The kernel
speaks only this contract: no Agent-CLI specifics (flag names, output formats, auth) appear
in the executor, State, or Events.

- **`AgentInvocation`** carries what a Node sends: `node_id`, `adapter`, `prompt`, `args`,
  the already-resolved `env` mapping, and optional `output_schema` / `fixture` paths.
- **`AgentResult`** is the normalized result: `exit_status`, `stdout`, `stderr`, optional
  parsed `structured_output`, and an `artifacts` tuple of produced files. The executor maps
  it onto the same `NodeResult` a shell Node yields, so the scheduler, State, and Events
  stay kind-agnostic.

An **`AdapterRegistry`** resolves a Node's `adapter` name to an Adapter instance and is the
sole dispatch seam: the executor routes on the Node's inputs type (shell -> subprocess,
agent -> registry lookup), so a new Agent CLI is a registry entry, not an executor edit.

Three contract rules bind every Adapter:

- An Agent CLI that ran and exited non-zero is an ordinary `AgentResult`, not an error;
  `AdapterError` is reserved for the Adapter failing to produce a result (unknown adapter,
  unreadable fixture), which the executor normalizes into a failed Node so the scheduler
  skips dependents uniformly.
- The Output Contract is the kernel's job, not the Adapter's: the kernel validates
  `structured_output` against `output_schema` after `invoke` returns and before dependents
  run. An Adapter may pass the schema to a CLI's structured-output feature, but the kernel
  re-validates regardless. The contract is evaluated only when the Agent CLI exited zero: it
  guards a successful invocation's output, and a non-zero exit is already a node failure, so
  re-checking would only risk masking the agent's own failure cause (#63). The structured
  output is validated as-is, including JSON null — the schema is the sole arbiter of whether
  null is allowed; the kernel never special-cases a `None` output as an automatic violation.
  Remote `$ref` resolution is disabled during validation, so an offline Run cannot egress on
  a fixture-controlled schema URL; an unresolvable reference is a contract error (#61).
- The `env` in `AgentInvocation` is the allow-list the kernel already filtered to declared,
  present names; the Adapter passes exactly that to the Agent CLI process and the kernel
  never persists its values (#5).

The env allow-list is **node-generic, not Agent-CLI-only**. Both an agent Node and a shell
Node declare `env` as a list of variable NAMES (never values), and the kernel resolves it
identically through one policy: a Node's process receives only the variables it declared and
that are present in the parent environment, the values never reach State, Events, or the
snapshot, and `AgentInvocation`'s repr redacts them (#5, #66, #65). A declared entry must be a
valid POSIX environment-variable name (`^[A-Za-z_][A-Za-z0-9_]*$`); a value-shaped entry such as
`API_TOKEN=s3cr3t`, an embedded `=`, a leading digit, or a space is rejected at normalize time,
so a secret value can never be smuggled into the allow-list and persisted into the snapshot
(#66). A declaring Node (agent or shell) is then responsible for listing every variable its
command/CLI needs, including `PATH` for a shell command's binaries — exactly the contract the
`claude.print` Adapter already documents — so an opted-in allow-list never silently leaks the
parent environment.

An OMITTED `env` and an EXPLICIT empty `env: []` are **distinct, not collapsed** (#66). An
agent Node's allow-list is always passed to the Agent CLI process, so for an agent Node both
omitted and empty mean "pass no declared variable" — an agent Node never inherits the parent
environment. A shell Node, which CAN inherit, honors the distinction: an OMITTED `env` inherits
the parent environment unchanged (the legacy default, so existing shell Workflows that rely on
ambient `PATH`/vars keep working), while an explicit empty `env: []` is a declared (empty)
allow-list and the shell receives **no variables at all** — a declaring node receives only its
declared-and-present variables, which for an empty list is none. To make the distinction
representable and survive a resume, the `env` field default is `None` (omitted), not `[]`, and
the normalized snapshot serializes an omitted `env` as `null` (distinct from `[]`), so a resume
reconstructs the SAME env scope rather than silently turning legacy inheritance into "pass no
vars".

The policy guards env INJECTION and kernel-held values; it is **not output redaction**. A Node
that echoes a secret into its own stdout or structured output — an Agent CLI printing a token,
or a shell command running `echo "$API_TOKEN"` — persists that value verbatim in State and the
trace. Keeping a secret out of a Node's output is the workflow author's responsibility, not the
kernel's: the allow-list controls what enters the process, never what the process chooses to
emit.

The v0.1 implementation is one `MockAdapter` that replays a fixture file as an
`AgentResult`, so Workflows and Patterns run with no Agent CLI installed.

## Considered Options

- **A concrete per-CLI base class instead of a small data-class interface** — rejected: it
  would invite CLI-shaped fields (model name, token budget, flag maps) into the shared
  contract and leak vendor specifics into the kernel, the coupling ADR 0001 forbids.
- **The Adapter validating its own Output Contract** — rejected: validation is a kernel
  guarantee that must hold identically across Adapters and even for a CLI with no
  structured-output feature, so it lives once in the kernel.
- **An `if/elif` dispatch on `node.kind` in the executor** — rejected: every new kind would
  edit the executor. A registry keyed by adapter name (and a kind-typed inputs dispatch)
  keeps the executor closed to modification.

## Consequences

- #9 and #11 implement `Adapter.invoke` for real CLIs and register under their names; no
  executor, State, or Event change is needed.
- The mock Adapter is an offline test seam that **complements** real agent-CLI e2e (#86),
  not a replacement for it: use the mock where a behavior can be verified completely and
  deterministically offline (our-own-logic, edge/error branches), and the e2e tier for
  behaviors whose correctness depends on the real CLI. The two are co-weighted — neither is
  a privileged "primary" seam. (Corrected from an earlier "permanent" framing that
  over-weighted the mock and let real-CLI coverage lapse.)
- `AgentResult.artifacts` is indexed minimally in State now; the full artifact lifecycle
  (collection, cleanup, retention) is deferred to #16.
- The data-class contract is internal, not a published plugin API; it may evolve as real
  Adapters land, with this ADR updated rather than versioned.
