"""The on-disk layout of run directories — the single source for their paths (#12).

A Run persists under ``<base>/.caw/runs/<run_id>/``. ``run`` and ``resume`` write
there, ``report`` reads from it; centralizing the layout here keeps the path from
being re-spelled at each call site (#31).
"""

from pathlib import Path

_RUNS_SUBPATH = (".caw", "runs")


def runs_root(base: Path | None = None) -> Path:
    """The directory holding every Run's directory, under ``base`` (default: cwd)."""
    return (base if base is not None else Path.cwd()).joinpath(*_RUNS_SUBPATH)


def run_dir(run_id: str, base: Path | None = None) -> Path:
    """The directory of one Run: ``<base>/.caw/runs/<run_id>``."""
    return runs_root(base) / run_id
