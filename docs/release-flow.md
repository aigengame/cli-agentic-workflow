# CI and release flow

GitHub Actions automation lives in `.github/workflows/`. Its contract is pinned by
`tests/test_github_workflows.py`, so changes to triggers, the Python matrix, or the
quality gates must update those tests too.

## CI quality gates

`ci.yml` runs on every pull request and every push to `main`, on Python 3.12 and 3.13:

```bash
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

A red gate blocks the PR with a failed check.

## Releasing

The whole release lifecycle lives in one workflow, `release.yml`, with
release-please as the single version authority (see
[ADR 0005](adr/0005-release-and-versioning-model.md)). Releases are driven by
[Conventional Commits](https://www.conventionalcommits.org/).

### Automatic path (default)

1. Conventional commits land on `main`; the `release-please` job opens or updates
   a Release PR with the version bump and the generated `CHANGELOG.md`.
   The `python` release type bumps both `[project].version` in `pyproject.toml`
   and `__version__` in `src/caw/__init__.py`; `.release-please-manifest.json`
   tracks the released version. A `sync-lockfile` job re-locks `uv.lock` on the
   Release PR branch so the PR's `uv sync --locked` gate passes.
2. Merging the Release PR makes release-please create the `v*` tag and the
   GitHub Release.
3. The `build` job — a downstream `needs: release-please` job in the same run —
   builds the sdist and wheel with `uv build` and attaches them to the Release.

No `workflow_call` indirection is needed: with the raw `push: tags` trigger gone,
the build is simply the back half of the release run.

### Manual path (escape hatch)

A `workflow_dispatch` with a required `tag` input re-builds and re-attaches
artifacts to an **existing** release — an operations-recovery path for a lost or
corrupt upload. It computes no version and bumps nothing: no `pyproject`,
`__init__`, manifest, or new tag. There is deliberately no raw `push: tags`
trigger, because a human-chosen tag name and the version `uv build` reads from
`pyproject.toml` are never reconciled, and a manual tag would be invisible to
release-please's manifest.

### Double-run guard

The `build` job is the only build trigger, so one release builds exactly once and
a raw tag push fires nothing. The build is also idempotent in case an automatic
build and a later manual rebuild of the same release overlap — the Release is only
created when missing (`gh release view || gh release create`) and uploads overwrite
with `--clobber` — and a concurrency group keyed on the tag serializes runs.

## Out of scope

Publishing to PyPI is a follow-up (issue #34); this flow stops at GitHub Release
artifacts.
