"""Run-group directory layout: the single owner of grouped-run paths (#15).

A Run Group (ADR 0002) materializes successive immutable Runs and persists
controller state alongside them. ``runlayout`` is the single source of every
path under a group dir, exactly as it owns single-Run paths (#12, #31): the
controller, ``caw report``, and resume read these helpers rather than
re-spelling the layout literals.
"""

from pathlib import Path

from caw import runlayout


def test_groups_root_sits_beside_runs_root_under_caw(tmp_path: Path) -> None:
    # Groups live under ``<base>/.caw/groups`` — a sibling of ``<base>/.caw/runs``
    # — so a group dir and a single-run dir never collide and either is locatable
    # from ``base`` alone.
    assert runlayout.groups_root(tmp_path) == tmp_path / ".caw" / "groups"
    assert runlayout.groups_root(tmp_path).parent == runlayout.runs_root(tmp_path).parent


def test_group_dir_is_one_group_under_groups_root(tmp_path: Path) -> None:
    group_dir = runlayout.group_dir("grp-1", tmp_path)
    assert group_dir == tmp_path / ".caw" / "groups" / "grp-1"


def test_group_iterations_root_holds_the_per_iteration_runs(tmp_path: Path) -> None:
    # Each iteration is an ORDINARY run directory minted by ``execute_run`` under
    # the group's ``iterations`` root, so the per-iteration layout (state.sqlite,
    # events.jsonl, workflow.normalized.json) is identical to a standalone run
    # and ``caw report`` / resume read it with the existing machinery.
    iterations_root = runlayout.group_iterations_root("grp-1", tmp_path)
    assert iterations_root == tmp_path / ".caw" / "groups" / "grp-1" / "iterations"
    assert iterations_root.parent == runlayout.group_dir("grp-1", tmp_path)


def test_group_state_path_is_the_controller_state_file(tmp_path: Path) -> None:
    # Controller state (iteration index, done Predicate inputs, per-iteration run
    # ids) is persisted in one JSON file the group dir owns (#15 / ADR 0002).
    state_path = runlayout.group_state_path("grp-1", tmp_path)
    assert state_path == tmp_path / ".caw" / "groups" / "grp-1" / "group.json"
    assert state_path.parent == runlayout.group_dir("grp-1", tmp_path)
