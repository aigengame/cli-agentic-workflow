"""The status vocabulary: the single owned set of Run and Node status strings (#30).

Run and Node lifecycle/terminal statuses live here as named constants so the
executor, State, Reporters, and CLI consume one vocabulary instead of hardcoding
the strings at each site (a property here, a SQL literal there). The set has one
home to extend — e.g. the Human Gate's parked/awaiting/rejected (ADR 0010) — rather
than scattered literals.

``failed`` / ``timed_out`` / ``errored`` double as the Error Classification failure
kinds a failed Node Attempt carries (CONTEXT.md). Skip *causes*
(blocked/when_false/all_branches_skipped) are a separate vocabulary owned by the
scheduler, and group statuses (done/exhausted/...) are owned by the Pattern
Controller; this module owns only Run and Node status.
"""

# A Node is in flight (``running``) or reached a terminal outcome: ``succeeded``,
# one of the failure kinds (``failed`` / ``timed_out`` / ``errored``), or
# ``skipped`` (never attempted). A Run is in flight (``running``), finished cleanly
# (``succeeded``), finished with a failed Node (``failed``), or was prevented from
# producing a result by an Adapter/internal fault (``errored``).
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
TIMED_OUT = "timed_out"
ERRORED = "errored"
SKIPPED = "skipped"
