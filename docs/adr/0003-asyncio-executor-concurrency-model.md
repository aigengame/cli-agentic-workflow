# ADR 0003: Asyncio Event Loop as the v0.1 Executor Concurrency Model

Status: Accepted
Date: 2026-06-11
Related: `docs/adr/0001-local-first-python-bash-workflow-kernel.md`,
`docs/prd/0001-cli-agentic-workflow.md`, issues #4, #6, #10

The executor runs as a single-threaded asyncio event loop. Each node attempt is an asyncio
task; subprocesses run through asyncio's subprocess support with streamed stdout/stderr
capture; timeouts and cancellation use task cancellation plus process termination. The
workload is long-running subprocess I/O at conservative concurrency, where the decisive
requirement is clean timeout, cancel, and park semantics — first-class asyncio operations —
and a single-threaded loop serializes state-store writes without locks.

## Considered Options

A thread-pool executor (`concurrent.futures`) was rejected: Python threads cannot be
cancelled, so node cancellation and timeout enforcement degrade into cooperative signaling
conventions, and shared run state would need locking discipline throughout the kernel.

## Consequences

- The executor code path is async end-to-end; executor tests use an async test runner.
- `python` nodes run synchronous user functions in a worker thread and therefore cannot be
  forcibly cancelled mid-flight; their cancellation is cooperative and takes effect at node
  boundaries. This asymmetry with subprocess nodes must be documented to workflow authors.
- The GIL is irrelevant to the subprocess-heavy workload; CPU-bound `python` nodes occupy
  worker threads, not the scheduler loop.
