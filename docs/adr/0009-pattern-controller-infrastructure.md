# ADR 0009: Pattern Controller Infrastructure — Run Groups, Structured Feedback, and `caw loop`

Status: Accepted
Date: 2026-06-16
Related: `docs/adr/0002-pattern-iteration-as-run-groups.md`,
`docs/adr/0008-pattern-expanders-compile-to-plain-ir.md`, `CONTEXT.md`, issue #15

ADR 0002 decided that every pattern iteration is a separate immutable Run and that a
Pattern Controller evaluates the finished Run N and materializes Run N+1, linking them
into a Run Group. ADR 0008 then realized the OTHER pattern axis — Pattern Expanders that
compile to one Run's plain IR — and explicitly left the Controller axis (run groups,
iteration index, stop conditions) as "a heavier, distinct axis." This ADR records HOW the
Controller axis is built, proven by `loop_until_done`, and why each mechanism is shaped as
it is. The kernel stays ignorant of controllers: `execute_run`/`resume_run` are unchanged
and a Controller drives them as black boxes from Python, never from the IR.

## Decisions

### A Controller is not an Expander and not an IR block

A Controller sequences multiple Runs; it is not registered in the expander registry
(ADR 0008) and there is no `controller:` block the kernel's `normalize_workflow` must
understand. Pushing controller semantics into the workflow schema would make the kernel
interpret patterns, exactly what ADR 0008 forbids. The iteration workflow file stays an
ordinary single-iteration `Workflow` (`nodes:` or `pattern:`); the loop is described by a
SEPARATE controller spec file the `caw loop` command consumes.

### Controller spec file (the loop's authored surface)

A small YAML spec carries the structured loop definition, so it is as inspectable as the
workflow it drives:

- `workflow`: path to the iteration `Workflow` file (an ordinary single-iteration graph).
- `max_iterations`: the hard upper bound (the loop stops at it even if never "done").
- `done`: a structured `Predicate` (the existing `when` algebra, ADR 0007) evaluated
  against the finished Run's named node output. CONTEXT.md makes the Predicate the SOLE
  conditional mechanism and "a composable structured algebra, not an expression string";
  the stop condition reuses it verbatim rather than inventing a stop-condition DSL.
- `evaluate_node`: the id of the Run's node whose normalized output the `done` predicate
  and the feedback source read. A Run can have several leaf nodes, so the spec names the
  one that carries the iteration's verdict explicitly, mirroring how a `when` ref names a
  node.
- `feedback` (optional): structured feedback injection — `to_node` + `to_field`. After
  iteration N, the Controller reads `evaluate_node`'s `structured_output` and substitutes
  it into the named field of the named node BEFORE materializing iteration N+1.

### Feedback is structural substitution, not string templating

Feedback from iteration N reaches iteration N+1 (AC2) by the Controller replacing a
NAMED node's NAMED input field with the prior Run's terminal `structured_output`, then
re-running `normalize_workflow` — the ADR 0008 path. It does NOT scan prompt/command text
for `${...}` tokens. The whole codebase is built on "no string eval / structured algebra"
(CONTEXT.md's Predicate entry); a `${feedback}` interpolation surface would be a new,
undocumented expression language that also collides with literal `${...}` a user may want
in a shell command, and would need its own escaping rules and ADR. Structural substitution
into a named field is collision-free and keeps the kernel ignorant of feedback. Iteration 1
uses the base workflow unchanged (no prior output to feed).

Because each iteration is normalized and frozen AFTER feedback is baked in, every iteration
is a DISTINCT immutable Run with its own `definition_checksum` and `workflow.normalized.json`
snapshot — so per-iteration immutability (ADR 0002) and resume's checksum re-validation hold
unchanged.

### Run Group directory layout (owned by `runlayout`)

A Run Group persists under `<base>/.caw/groups/<group_id>/` — a sibling of the single-Run
`.caw/runs/` root so a group and a standalone run never collide:

- `group.json`: the persisted controller state — group id, the spec, the iteration index,
  and the ordered per-iteration run ids and statuses. This is the AUTHORITATIVE source of
  the Run Group's control flow and resumption (ADR 0002).
- `iterations/`: the runs root passed to `execute_run`, so each iteration materializes as
  an ORDINARY run directory (`state.sqlite`, `events.jsonl`, `workflow.normalized.json`)
  beneath it — identical to a standalone run, read by the same report/resume machinery.

`runlayout` is the single owner of these paths (`groups_root`, `group_dir`,
`group_iterations_root`, `group_state_path`), as it already owns single-Run paths (#12,
#31). The Controller reconstructs an iteration's run dir as
`group_iterations_root(group_id) / run_result.run_id`, since `execute_run` mints the run id
internally and run dirs sit directly under the runs root it was given — so no `run_dir`
field is added to `RunResult`.

### Run State records membership (a queryable mirror)

AC3 requires the Run itself to record its run group id and iteration index. The Controller
writes a row into an ADDITIVE `run_group_membership(run_id, run_group_id, iteration_index)`
table (a NEW table via `CREATE TABLE IF NOT EXISTS`, never altering existing tables, #76
lesson) by re-opening the iteration's already-finalized State. The table is a denormalized
MIRROR; `group.json` remains authoritative for control flow, so group resume trusts
`group.json` and tolerates a missing membership row.

### Loop termination

The loop stops (AC4) when ANY holds, checked after each finished iteration: the iteration's
Run FAILED (no point feeding a failed result forward), the `done` predicate evaluated true
over `evaluate_node`'s output, or the iteration index reached `max_iterations`. The group
status records which: `failed`, `done`, or `exhausted`.

### Group-level resume (the Run Group is the resumption unit)

A Run Group resumes (AC5) by re-reading `group.json`: a SUCCEEDED iteration Run is never
re-run (Resume Eligibility, CONTEXT.md), an incomplete last iteration is `resume_run`'d in
place, and the loop then continues from the persisted iteration index. The kernel's
`is_resumable` rule is honored per iteration. Resuming a single iteration by raw run id
through `caw resume` is intentionally NOT supported: iterations live under the group dir,
not `.caw/runs/`, so `caw resume <iteration_run_id>` finds no run dir — the Run Group, not
a lone iteration, is the resumption unit (ADR 0002).

### CLI surface: a separate `caw loop` sub-typer

`caw loop run <spec>`, `caw loop resume <group_id>`, and `caw loop report <group_id>` form
a sub-typer (mirroring `caw patterns`), leaving `caw run`/`caw resume`/`caw report`
UNTOUCHED — no group-vs-run id detection heuristic, no strained "which id is missing" error.
Exit codes mirror the single-run contract: 0 (group done), 1 (a constituent Run failed),
2 (config refusal: bad spec, unknown/unresumable group), 3 (infrastructure). A group report
is a DISTINCT aggregate shape (`render_group_report`), not a thin overload of the single-run
`render_report`: it wraps each iteration's per-run report (conclusion + trace evidence) into
one Run Group result.

## Considered Options

- **A `${feedback}` string-templating placeholder** — rejected: it introduces an
  undocumented expression surface against the codebase's structured-algebra grain, collides
  with literal `${...}`, and needs escaping rules and its own ADR. Structural substitution
  into a named field is collision-free.
- **A `controller:` block inside the workflow file** — rejected: the kernel would have to
  understand controllers in `normalize_workflow`, which ADR 0008 forbids ("plain IR keeps
  the kernel ignorant of patterns"). A separate spec file keeps the workflow ordinary.
- **Iterations under `.caw/runs/` + a group-vs-run detection heuristic on `caw report`** —
  rejected: it tangles group resume with single-run resume and makes aggregate reporting
  hunt a shared directory, and the detection heuristic strains the one-`error:`-line exit
  contract. A dedicated group dir and a dedicated `caw loop` namespace are unambiguous.
- **`run_group_id`/`iteration_index` only in `group.json` (no run-State row)** — rejected:
  AC3 requires the Run itself to record them. The additive mirror table satisfies that
  without making the table authoritative.

## Consequences

- The kernel, executor, validation, checksum, and single-run resume gain NO controller
  surface: a Controller is Python orchestration above `execute_run`, driving immutable Runs.
- `loop_until_done` is the first Controller; further controllers (tournament rounds,
  regenerate-until) reuse the Run Group layout, the membership mirror, and the `caw loop`
  surface, adding their own materialization/stop logic.
- Single-iteration resume/report by raw run id is intentionally out of scope; the Run Group
  is the unit, consistent with ADR 0002.
