# ADR 0007: Node `when` Predicates Are a Composable Structured Algebra; Join Tolerates Skips, Never Failures

Status: Accepted
Date: 2026-06-13
Related: `docs/adr/0002-pattern-iteration-as-run-groups.md`,
`docs/adr/0003-asyncio-executor-concurrency-model.md`,
`docs/adr/0006-adapter-interface-contract.md`, `CONTEXT.md`, issue #7

CONTEXT.md makes a Node's `when` the sole conditional mechanism — Edges express ordering
and data flow only, and conditional behavior lives in a Node's `when` predicate, never on an
Edge. This records how `when` is shaped, how a closed gate and a tolerant join interact with
the existing failure-skip semantics (#4 / ADR 0006), and why.

A **Predicate** is a recursive, composable boolean algebra, modelled structurally rather
than as an expression string:

- an atomic **leaf** — a `ref` to one field of an upstream Node's normalized output
  (`stdout`, `exit_status`, or `structured_output`), an `op`, and a `value` (one reference →
  comparison, the indivisible atom);
- the combinators `all_of` / `any_of` — a list of sub-predicates combined by AND / OR;
- `not` — a single negated sub-predicate.

A predicate is EXACTLY one shape — a leaf XOR one combinator — validated in the model.
Combinators nest arbitrarily, so the algebra is composable. v0.1 implements the operators
`equals` and `contains` (a substring test, valid only on the string `stdout` field) plus all
three combinators. Every leaf `ref.node` must appear in the owning Node's `needs`, so a
`when` reads only outputs guaranteed present at evaluation time and adds no Edges — the
concrete Run's IR stays acyclic (ADR 0002). The kernel evaluates a `when` by walking the
validated model directly (`caw.predicate.evaluate_predicate`): there is no parser and no
`eval`, so the only conditional surface is the typed algebra. A Node whose `when` evaluates
false is marked `skipped` and never executed.

A Node also declares a **join policy** on a separate axis from `when`: `all` (the default)
skips the Node if ANY dependency skipped; `any` tolerates skipped upstream branches — the
Node runs iff at least one dependency executed and succeeded, and is itself skipped (cause
`all_branches_skipped`) only if ALL dependencies skipped. The load-bearing invariant: a
FAILED dependency blocks dependents REGARDLESS of join policy. **Join tolerates skips, never
failures.** The skip-origin walk consults `join`; the failure-origin walk never does, so a
failed branch blocks even a `join: any` join — the discriminating case that keeps a tolerant
join from masking a failure.

Each skip carries a named cause so a Reporter renders it distinctly from success and
failure: `when_false` (a closed gate), `blocked` (a failed or skipped dependency withheld
it, with the blocker named), or `all_branches_skipped` (a fully-skipped tolerant join). The
JSON plan emits each Node's `when` and `join`; the text plan annotates them.

## Considered Options

- **An expression string (e.g. `classify.stdout == "ship" and not flagged`)** — rejected: it
  needs a parser and an evaluator, which is a new conditional surface to specify, secure, and
  test (injection, partial evaluation, error reporting on malformed expressions). A
  structured algebra is validated by the model, evaluated by walking it, and serializes
  losslessly into the run snapshot with no separate grammar.
- **Conditions on Edges** — rejected: forbidden by the domain language. CONTEXT.md defines an
  Edge as ordering and data flow only, with conditional behavior living in a Node's `when`.
  An edge condition would also be a second, overlapping conditional mechanism.
- **A single non-recursive predicate (one leaf, no combinators)** — rejected: classify-and-act
  and generate-and-filter shapes routinely need AND / OR / NOT over several upstream fields.
  Forcing authors to introduce intermediate Nodes to combine conditions would distort the
  graph for a purely expressive gap.
- **A `join: any` that tolerates failures as well as skips** — rejected: it would let a
  tolerant join run (and the Run succeed) over a FAILED branch, silently masking the failure.
  Join is the skip-tolerance axis; failure semantics (#4 / ADR 0006) stay authoritative.

## Consequences

- The algebra is extensible by design: new operators (`not_equals`, `gt`, `matches`, `in`),
  new ref fields, or new combinators are added by widening the model's `Literal`s or adding a
  shape, with no restructuring of evaluation, scheduling, or serialization.
- `when` adds no Edges, so the IR stays acyclic and the existing ordering, validation,
  resume, and checksum machinery is unchanged.
- A `when` evaluates identically in a fresh Run and a resumed one: on resume a dependency
  that was a prior success is read back from State, so a conditional workflow resumes
  correctly.
- A benign skip (a closed gate or a fully-skipped tolerant join) does not fail the Run; only
  a failed Node does. The Run-success test stays "all attempted Nodes succeeded".
- Reporting deferred to #12 (the four-format Reporters) consumes the per-Node `skipped_causes`
  already recorded here; this ADR adds only the minimal distinct surfacing in `caw graph` and
  the run summary.
