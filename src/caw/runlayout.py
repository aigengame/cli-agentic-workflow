"""The on-disk layout of run directories — the single source for their paths (#12).

A Run persists under ``<base>/.caw/runs/<run_id>/``. ``run`` and ``resume`` write
there, ``report`` reads from it; centralizing the layout here keeps the path from
being re-spelled at each call site (#31).

A Run Group (ADR 0002, #15) persists under ``<base>/.caw/groups/<group_id>/``: a
``group.json`` holding the controller's persisted state, and an ``iterations/``
root the Pattern Controller passes to ``execute_run`` so each iteration is an
ORDINARY run directory minted beneath it. The group dir is a sibling of the
single-Run ``runs`` root, so a group and a standalone run never collide and the
per-iteration layout is identical to a standalone run's — ``caw report`` and
resume read it with the same machinery. This module is the single owner of both
layouts so neither is re-spelled at a call site.
"""

from pathlib import Path

_RUNS_SUBPATH = (".caw", "runs")
_GROUPS_SUBPATH = (".caw", "groups")
# The controller-state file inside a group dir, and the per-iteration runs root.
_GROUP_STATE_FILENAME = "group.json"
_GROUP_ITERATIONS_SUBDIR = "iterations"


def runs_root(base: Path | None = None) -> Path:
    """The directory holding every Run's directory, under ``base`` (default: cwd)."""
    return (base if base is not None else Path.cwd()).joinpath(*_RUNS_SUBPATH)


def run_dir(run_id: str, base: Path | None = None) -> Path:
    """The directory of one Run: ``<base>/.caw/runs/<run_id>``."""
    return runs_root(base) / run_id


def groups_root(base: Path | None = None) -> Path:
    """The directory holding every Run Group's directory, under ``base`` (#15).

    A sibling of :func:`runs_root` under ``<base>/.caw`` so a group dir and a
    single-Run dir never collide.
    """
    return (base if base is not None else Path.cwd()).joinpath(*_GROUPS_SUBPATH)


def group_dir(group_id: str, base: Path | None = None) -> Path:
    """The directory of one Run Group: ``<base>/.caw/groups/<group_id>`` (#15)."""
    return groups_root(base) / group_id


def group_iterations_root(group_id: str, base: Path | None = None) -> Path:
    """The runs root a Run Group's iterations are minted under (#15).

    Passed to ``execute_run`` as its ``runs_root`` so each iteration materializes
    as an ordinary run directory beneath it, with the same layout (state.sqlite,
    events.jsonl, workflow.normalized.json) a standalone run has.
    """
    return group_dir(group_id, base) / _GROUP_ITERATIONS_SUBDIR


def group_state_path(group_id: str, base: Path | None = None) -> Path:
    """The controller-state file of a Run Group: ``<group_dir>/group.json`` (#15).

    Persists the controller's iteration index, stop-condition inputs, and the
    ordered per-iteration run ids — the Run Group's resumable state (ADR 0002).
    """
    return group_dir(group_id, base) / _GROUP_STATE_FILENAME
