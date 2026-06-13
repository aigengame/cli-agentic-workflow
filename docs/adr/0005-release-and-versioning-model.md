# ADR 0005: Release and Versioning Model

Status: Accepted
Date: 2026-06-13
Related: `docs/adr/0004-python-stack-and-toolchain.md`, issue #33 (release pipeline),
issue #51 (single version authority)

release-please is the single authority for the project version, and the entire release
lifecycle lives in one workflow run. This replaces a two-mode design whose manual path held
a second, unreconciled version authority. Recorded here so the release path is not
re-litigated when someone reaches for a quick manual tag.

- **release-please owns the version.** On push to `main` it derives the next version from
  `.release-please-manifest.json` plus the conventional commits since the last release and
  maintains a Release PR that bumps `pyproject.toml` and `src/caw/__init__.py`. The PR also
  re-locks `uv.lock` (which embeds the project's own `caw` version) on its own branch so the
  PR's `uv sync --locked` gate sees a matching lockfile. Merging the Release PR creates the
  tag and the GitHub Release. Nothing else may compute or assert a version.
- **The build is the back half of one release run**, not a separate workflow. With the raw
  tag-push trigger gone (below), a release-please tag created with the default `GITHUB_TOKEN`
  no longer needs the `workflow_call` indirection that existed only to dodge the tag-push
  anti-recursion rule. So `release-please.yml` and `release-build.yml` are merged into one
  `release.yml`, and the build is a downstream `needs: release-please` job that runs once per
  release. The anti-double-run property is structural: one build trigger, one build.
- **The manual escape hatch is `workflow_dispatch` over an existing tag, and bumps nothing.**
  It re-builds and re-attaches artifacts (`--clobber`) to a release that already exists — an
  operations-recovery path for a lost or corrupt upload. It takes the tag as a required input,
  computes no version, and never writes `pyproject`, `__init__`, the manifest, or a new tag.
- **Raw `push: tags: v*` triggering was deliberately removed.** Under it the human-chosen tag
  name and the version `uv build` reads from `pyproject.toml` at the tagged commit were never
  reconciled, so a tag could ship artifacts of a different version; worse, a manual tag was
  invisible to release-please's manifest, letting the next automatic release compute a
  regressing or colliding version. Removing it collapses the two authorities into one.
- **Hardening is retained from the prior workflows:** SHA-pinned actions, least-privilege
  per-job `permissions`, and a build `concurrency` group keyed on the tag so an automatic
  build and a later manual rebuild of the same release cannot race each other's uploads.
