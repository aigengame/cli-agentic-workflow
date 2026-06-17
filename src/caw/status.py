"""The status vocabulary: the single owned set of Run and Node status strings (#30).

Run and Node statuses live here once, exposed two ways:

- named constants (``RUNNING`` … ``SKIPPED``) for the call sites that build or compare
  a status;
- ``RunStatus`` / ``NodeStatus`` Literal aliases for the shared APIs that carry a status
  across modules — ``NodeResult.status`` / ``RunResult.status`` and
  ``StateStore.record_run_finished`` / ``record_node_finished`` — so a status typo is a
  static error at those boundaries (mypy --strict checks src and tests), not merely a
  string consolidated into one file.

``cancelled`` is part of the Run status vocabulary per ``CONTEXT.md`` (Resume Eligibility
lists it among the resumable interrupted runs). The v0.1 kernel currently finalizes an
interrupted/cancelled Run as ``errored`` and does not yet EMIT ``cancelled`` itself, but the
owner carries it so the typed vocabulary stays faithful to the glossary and is ready when
cancellation handling emits it.

``failed`` / ``timed_out`` / ``errored`` double as the Error Classification failure kinds a
failed Node Attempt carries (``FailureKind``). Skip *causes* (blocked/when_false/
all_branches_skipped) are a separate vocabulary owned by the scheduler, and group statuses
(done/exhausted/...) are owned by the Pattern Controller; this module owns only Run and Node
status. New statuses extend it in one place — e.g. the Human Gate's parked/awaiting/rejected
(#10).
"""

from typing import Final, Literal

# Each constant's value is also a member of the Literal alias below; keep the two in sync.
RUNNING: Final = "running"
SUCCEEDED: Final = "succeeded"
FAILED: Final = "failed"
TIMED_OUT: Final = "timed_out"
ERRORED: Final = "errored"
CANCELLED: Final = "cancelled"
SKIPPED: Final = "skipped"

# A Run is in flight (``running``), finished cleanly (``succeeded``), finished with a failed
# Node (``failed``), was prevented from producing a result by an Adapter/internal fault
# (``errored``), or was cancelled (``cancelled``).
RunStatus = Literal["running", "succeeded", "failed", "errored", "cancelled"]

# A Node is in flight (``running``) or reached a terminal outcome: ``succeeded``, one of the
# failure kinds (``failed`` / ``timed_out`` / ``errored``), or ``skipped`` (never attempted).
NodeStatus = Literal["running", "succeeded", "failed", "timed_out", "errored", "skipped"]

# The Error Classification failure kinds a failed Node Attempt carries (a subset of
# NodeStatus); ``None`` on a NodeResult means the Attempt succeeded.
FailureKind = Literal["failed", "timed_out", "errored"]
