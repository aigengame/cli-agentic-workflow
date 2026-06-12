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

Releases are driven by [Conventional Commits](https://www.conventionalcommits.org/)
through two co-existing trigger paths that converge on one build workflow.

### Automatic path (default)

1. Conventional commits land on `main`; `release-please.yml` opens or updates a
   Release PR with the version bump and the generated `CHANGELOG.md`.
   The `python` release type bumps both `[project].version` in `pyproject.toml`
   and `__version__` in `src/caw/__init__.py`; `.release-please-manifest.json`
   tracks the released version.
2. Merging the Release PR makes release-please create the `v*` tag and the
   GitHub Release.
3. The same workflow then calls `release-build.yml` (via `workflow_call`), which
   builds the sdist and wheel with `uv build` and attaches them to the Release.

The explicit hand-off exists because the tag is created with the default
`GITHUB_TOKEN`, and GitHub never lets such events trigger other workflows.

### Manual path (escape hatch)

Pushing a `v*` tag by hand fires the `push: tags` trigger of `release-build.yml`
directly, with no release-please involvement. The workflow creates the GitHub
Release if the tag has none, then attaches the same artifacts. Note that the
manual path does not bump versions or the changelog — the tag releases whatever
the tagged commit contains.

### Double-run guard

Both paths reach `release-build.yml` exactly once: token-created tags cannot fire
`push: tags`, and manual tags never run release-please. If the paths ever race
anyway (for example after switching release-please to a PAT), the build is
idempotent — the Release is only created when missing (`gh release view ||
gh release create`) and uploads overwrite with `--clobber` — and a concurrency
group serializes runs per tag.

## Out of scope

Publishing to PyPI is a follow-up (issue #34); this flow stops at GitHub Release
artifacts.
