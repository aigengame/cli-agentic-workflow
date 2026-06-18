# ADR 0006: The Adapter Interface Contract

Status: Accepted
Date: 2026-06-13
Related: `docs/adr/0001-local-first-python-bash-workflow-kernel.md`,
`docs/adr/0003-asyncio-executor-concurrency-model.md`,
`docs/adr/0004-python-stack-and-toolchain.md`, issues #5, #9, #11, #16, #66, #79, #83, #84

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
  parsed `structured_output`, an `artifacts` tuple of produced files, and an
  `adapter_failure` flag (the adapter-determined-failure signal, below). The executor maps
  it onto the same `NodeResult` a shell Node yields, so the scheduler, State, and Events
  stay kind-agnostic.

An **`AdapterRegistry`** resolves a Node's `adapter` name to an Adapter instance and is the
sole dispatch seam: the executor routes on the Node's inputs type (shell -> subprocess,
agent -> registry lookup), so a new Agent CLI is a registry entry, not an executor edit.

Four contract rules bind every Adapter:

- An Agent CLI that ran and exited non-zero is an ordinary `AgentResult`, not an error;
  `AdapterError` is reserved for the Adapter failing to produce a result (unknown adapter,
  unreadable fixture), which the executor normalizes into a failed Node so the scheduler
  skips dependents uniformly.
- An **adapter-determined failure** — the Agent CLI RAN but the Adapter normalized its
  result as a FAILURE, the canonical case being Claude's `is_error: true` arriving with a
  zero process exit — is signalled by the first-class `AgentResult.adapter_failure` flag,
  NOT by manufacturing a non-zero `exit_status` (#83). The Adapter keeps the process's REAL
  exit status in `exit_status` (so the trace stays honest about what the process did) and
  raises the flag; the kernel honors it ONCE — a zero-exit result carrying `adapter_failure`
  is a failed Node, exactly as a non-zero exit is — so every real Adapter (claude #9, codex
  #11, the usage work #79) signals an agent-determined failure the same way rather than each
  re-inventing the convention through the exit-code channel. A failed node carries no
  trustworthy structured output, so the Adapter drops it and puts the cause on `stderr`. This
  is distinct from a kernel-determined Output-Contract breach, which has no real non-zero
  process exit to preserve and so is recorded as `exit_status = 1` (#63).
- The Output Contract is the kernel's job, not the Adapter's: the kernel validates
  `structured_output` against `output_schema` after `invoke` returns and before dependents
  run. An Adapter may pass the schema to a CLI's structured-output feature, but the kernel
  re-validates regardless. The contract is evaluated only when the invocation SUCCEEDED —
  the Agent CLI exited zero AND `adapter_failure` is not set: it guards a successful
  invocation's output, and any failure (non-zero exit or adapter-determined) is already a
  node failure, so re-checking would only risk masking the agent's own failure cause (#63).
  The structured
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

The first implementations are the `MockAdapter` that replays a fixture file as an
`AgentResult` (so Workflows and Patterns run with no Agent CLI installed) and the real
`claude.print` Adapter (#9). The per-CLI subprocess machinery a real Adapter needs —
locating the CLI on PATH (resolved once, cached), spawning its absolute path with the
strict node `env` allow-list and isolated stdin, owning a process group so the whole tree
is killable+reapable on a timeout/cancellation, normalizing the returncode, and turning a
missing CLI into one actionable `AdapterError` — is the shared `SubprocessAdapter` base
(#83), which `codex.exec` (#11) reuses unchanged so a second CLI is argv construction plus
result-wrapper parsing, not a re-implementation of the lifecycle. The `node 'id' (adapter
'name')` diagnostic prefix and the read → `json.loads` → dict `AdapterError` ladder are
shared helpers used by both the real Adapters and `MockAdapter`, so a malformed fixture and
a malformed CLI wrapper surface the same shape of error. The version-probe `capability_check`
lives on that base (shared by claude and codex), not on the abstract `Adapter`, since the
mock has no CLI to probe; it has no production caller yet (its version surfaces via the usage
work #79 and is exercised by the real-CLI e2e #86 today).

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
- **Signalling an adapter-determined failure by manufacturing `exit_status = 1`** (the #9
  shipping behavior) — rejected for #83: overloading the exit-code channel makes the
  Adapter fabricate a code the kernel cannot tell from a real process exit, and bakes the
  convention into each Adapter (claude, then codex #11, then #79) so a fix or a richer
  failure shape would have to be re-applied everywhere. A dedicated first-class
  `AgentResult.adapter_failure` flag the kernel honors once keeps the real `exit_status`
  honest, lives in the shared contract, and extends cleanly (a future Adapter or richer
  failure-reason rides the same seam). A string `failure_reason` enum was considered and
  deferred: the boolean is the minimal representation the current callers need (the human
  cause already rides `stderr`), and it can grow into a structured reason later without a
  contract break. Raising a dedicated exception was rejected: `AdapterError` already means
  "the Adapter could not produce a result", whereas here the Adapter DID produce a normalized
  result that simply represents a failure — a returned value, not a control-flow break.

## Consequences

- #9 and #11 implement `Adapter.invoke` for real CLIs and register under their names; no
  executor, State, or Event change is needed.
- The mock Adapter is an offline test seam that **complements** real agent-CLI e2e (#86),
  not a replacement for it: use the mock where a behavior can be verified completely and
  deterministically offline (our-own-logic, edge/error branches), and the e2e tier for
  behaviors whose correctness depends on the real CLI. The two are co-weighted — neither is
  a privileged "primary" seam. (Corrected from an earlier "permanent" framing that
  over-weighted the mock and let real-CLI coverage lapse.)
- `AgentResult.artifacts` is the Adapter-to-kernel handoff for files a real writable
  Agent CLI run created or modified. Real subprocess Adapters discover changed regular
  files in the node-owned working directory the kernel assigns to that invocation, not
  the process's shared ambient cwd, and return those source paths; the kernel then
  collects them into the run directory under `artifacts/<node-id>/` and persists only
  those run-owned copies in State (#16). A Workflow may declare
  `artifact_cleanup.keep_last_runs` to prune old run artifact directories after a run
  finishes; the current run is always preserved, and the default is conservative (no
  cleanup).
- The `SubprocessAdapter` base (#83) is where a cross-cutting subprocess fix lands once
  rather than per-CLI: `codex.exec` (#11) sets its CLI name and missing-CLI hint and inherits
  the whole lifecycle. The base is NOT itself a registered Adapter — it has no `invoke`; the
  vendor-neutral kernel boundary is unchanged.
- An Adapter that asks a CLI for structured output passes the schema in the form the REAL CLI
  accepts: `claude --json-schema <schema>` takes the schema CONTENT inlined, not a file path
  (verified against the CLI for #84; there is no `--json-schema-file` flag). So the claude
  Adapter inlines the schema text into argv; passing a path is not an option the CLI supports,
  and the ARG_MAX exposure of a very large schema is inherent to that flag, not a caw choice.
  The kernel's own `validate_output_contract` reads the schema independently (and caches it),
  because the kernel re-validates regardless of what the Adapter passed the CLI — the
  separation is deliberate (a CLI with no structured-output feature is still contract-checked),
  so the two reads are not a redundancy to collapse.
- The data-class contract is internal, not a published plugin API; it may evolve as real
  Adapters land, with this ADR updated rather than versioned.
