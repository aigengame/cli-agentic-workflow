# ADR 0010: Await Parking and the Human Gate

Status: Accepted
Date: 2026-06-17
Related: `docs/adr/0003-asyncio-executor-concurrency-model.md`,
`docs/adr/0002-pattern-iteration-as-run-groups.md`,
`docs/adr/0007-when-predicates-and-skip-semantics.md`, `CONTEXT.md`, issues #10, #30, #6

PRD #1 makes the Human Gate the only Await trigger source required in v0.1: a `human_gate`
node parks the Run, and approval happens through an interactive TTY confirmation or
`caw resume <run-id> --approve <node-id>`. ADR 0003 already chose the asyncio executor for
its first-class park semantics but left the *workflow-level* await/approval model open. This
ADR records HOW await parking and the Human Gate are built on top of the existing Resume
machinery (#6), and why each mechanism is shaped as it is. The guiding result: approval and
rejection need almost no new kernel control flow — they reuse Resume's "seed satisfied"
classification — so the new surface is a node kind, two run statuses, two node statuses, and
a CLI flag pair, not a new execution path.

## Decisions

### Approval reuses the Resume pipeline; the kernel gains no "gate satisfied" re-entry

Resume already classifies a `succeeded` node as *seeded satisfied* — its dependents run
WITHOUT re-running it (`state.py` `node_statuses`, `executor.py` `resume_run`). Approval
exploits exactly this: `caw resume <run-id> --approve <node-id>` flips the named `awaiting`
gate node to `succeeded` BEFORE the normal resume classification runs, so its dependents
unlock through the unchanged seeding path. The executor needs no special "treat this gate as
satisfied" branch — approval is a one-row status flip plus an ordinary Resume. This keeps the
await mechanism a thin layer over #6 rather than a second scheduler entry point.

### Parking is a scheduler fixpoint, not a first-gate exit

A `human_gate` node, when the scheduler "launches" it, does NOT spawn a subprocess: it records
the node `awaiting` and is left in place (not terminal). The scheduler keeps advancing every
other ready node. The Run parks — persists State, records the park Events, and the process
exits cleanly in a non-TTY session — only at the FIXPOINT where no node can make further
progress, no node is in flight, and at least one node is `awaiting`. At that point SEVERAL
gates may be `awaiting` at once.

The rejected alternative — park and exit the instant the first gate is reached — was refused
because it would force-cancel in-flight sibling branches that could still make progress, which
contradicts the executor's "run everything ready" model (ADR 0003) and wastes parallel work.
Sequential gates fall out for free: approving a gate whose downstream reaches another gate
re-parks at that next gate on the following resume.

### Rejection is a decided, non-resumable terminal — distinct from cancelled and failed

Declining a gate is symmetric to approving it: `caw resume <run-id> --reject <node-id>` drives
the named gate node to `rejected` and the Run to `rejected`; the gate's dependents block-skip
under the existing "a failed dependency blocks dependents" rule (ADR 0007), and the Run ends.

`rejected` is a NEW status, not a reuse of `cancelled` or `failed`:

- Not `cancelled`: CONTEXT.md classes `cancelled` as an *interrupted* run that is resumable.
  A rejection is a *decided* outcome, semantically nearer `succeeded` ("the Run reached its
  terminal decision; the decision was no"). Making rejection resumable would let a later
  `--approve` override the human's no — a footgun. A `rejected` Run is therefore REFUSED by
  Resume Eligibility, like `succeeded`; to re-decide, start a new Run. This also keeps the
  Run an immutable record of the decision, consistent with ADR 0002.
- Not `failed`: a human "no" is not an error. Recording it as `failed` would make a Reporter
  render a legitimate decision as a failure. `rejected` lets reports say "not approved".

### Resume Eligibility: `parked` resumes, `rejected` is refused

A `parked` Run joins the resumable set: its Await is advanced by approving or declining an
`awaiting` node, not by re-running completed work. A plain `caw resume <run-id>` (no
`--approve`/`--reject`) on a run parked at a gate is a no-op advance — the gate is still
`awaiting`, so the run re-parks idempotently. `rejected` joins the refused set alongside
`succeeded`.

### Multi-gate resume is node-addressed and may name several gates at once

Because the fixpoint can leave multiple gates `awaiting`, `--approve`/`--reject` address a
specific node by id (which is why the PRD's surface is `--approve <node-id>`, not a bare
`--approve`). One resume invocation may carry several, e.g.
`caw resume <run-id> --approve g1 --reject g2`. After that resume advances, any gate left
unnamed stays `awaiting` and the Run re-parks. Approving and rejecting are the only two
operations on an `awaiting` node.

### `human_gate` node shape: a `prompt` in, an `approved` out, not yet a predicate source

`human_gate` is the third node kind (after `shell` and `agent`). Its inputs are minimal — a
single optional `prompt` shown at the TTY confirmation (`Approve deploy to prod? [y/N]`). The
subprocess-shaped fields (`timeout`, `retries`, `env`, `cwd`, `artifacts`, `output_schema`)
do not apply: a gate spawns no process. In particular a `timeout` that auto-rejects is a
*timer* Await, which PRD #1 places out of v0.1 scope.

On approval the gate emits `{"approved": true}` as its normalized output, so the decision is
inspectable in State, Events, and reports; on rejection the node is `rejected` with no
successful output. `approved` is deliberately NOT added to the `when`-producible fields for a
gate in v0.1: because "decline ends the Run", any node downstream of a gate only runs when
`approved` is true, so branch-on-decision has no v0.1 meaning. Expressing the decision as node
output (rather than only as an Event) keeps the primitive composable — enabling
branch-on-decision later is just adding `approved` to the producible map and relaxing
"decline ends the Run", with no change to the persisted data shape (primitive-based design).

### TTY confirmation vs non-TTY parking

In a TTY session, reaching a gate during `caw run` prompts inline; approving continues the run
in place, declining drives the `rejected` terminal. In a non-TTY session the gate parks and
the process exits cleanly, and the decision is supplied out of band by
`caw resume <run-id> --approve|--reject <node-id>`. The same `--approve`/`--reject` flags hang
off the EXISTING `caw resume` command — no new CLI namespace — since approval IS a resume.

### Status and event vocabulary land on #30's single source, which comes first

This ADR introduces node statuses `awaiting`/`rejected`, run statuses `parked`/`rejected`,
and event types `gate_awaiting`/`gate_approved`/`gate_rejected`. Those vocabularies are today
scattered raw strings and inline literals — exactly the append-conflict hotspot the repo's
parallel-development rules name and the subject of issue #30 (own status, event types, and
attempt numbers in one place). Per "land the base, then extend", #30 is sequenced FIRST as the
vocabulary base (it is small and unblocked); the Human Gate work (#10) then adds its members
in the single owned status enum and event-type vocabulary rather than sprinkling new literals
across the four sites #30 is consolidating. This keeps the authoritative source single and
prevents a typo'd new event type from passing type-checking.

## Considered Options

- **Park and exit at the first gate reached** — rejected: it must force-cancel in-flight
  sibling branches that could still progress, against the executor's run-everything-ready
  model (ADR 0003). The fixpoint park lets parallel branches advance to their own gates first.
- **A dedicated approve/re-entry path in the executor** — rejected: Resume's seed-satisfied
  classification already advances a graph past a node marked `succeeded`. Flipping the gate to
  `succeeded` and re-running Resume reuses that with no second scheduler entry point.
- **Reuse `cancelled` for rejection** — rejected: `cancelled` is an interrupted, resumable
  status; rejection is a decided, non-resumable one, and conflating them would let `--approve`
  override a human's no.
- **Reuse `failed` for a rejected gate** — rejected: a human "no" is not an error and must not
  render as a failure in reports.
- **Wire `approved` into `when`-producible fields now** — rejected: with "decline ends the
  Run", `approved` is always true downstream, so a v0.1 predicate on it is dead surface that
  would mislead authors. The field is emitted for the trace and kept out of predicates until
  branch-on-decision is a deliberate, separate extension.
- **A new `caw approve`/`caw gate` command** — rejected: approval is a resume, so the
  `--approve`/`--reject` flags belong on `caw resume`; a new namespace would duplicate the
  resume machinery.

## Consequences

- The executor, validation, checksum, and single-run Resume gain no new execution path: a gate
  parks the scheduler at a fixpoint, and approval/rejection are a status flip plus the
  unchanged Resume from #6.
- `parked` and `rejected` extend the run-status set and `awaiting`/`rejected` the node-status
  set; Resume Eligibility now refuses `rejected` (with `succeeded`) and admits `parked`.
  CONTEXT.md's Await, Human Gate, Resume Eligibility, and Error Classification entries are
  updated to carry these terms.
- #30 is the prerequisite for #10's vocabulary: it lands the owned status enum and typed
  event-type vocabulary first, and #10 extends them in one place.
- External-event Await triggers (files, webhooks, timers) remain out of v0.1 scope but reuse
  this same parking fixpoint and `parked` status when added; only the trigger source differs.
- A `human_gate` carries an optional `prompt` and emits `{"approved": true}`; enabling
  branch-on-decision later is additive (producible-field exposure + relaxing decline-ends-run)
  with no persisted-shape change.
