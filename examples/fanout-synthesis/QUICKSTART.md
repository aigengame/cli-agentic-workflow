# Quickstart: the fan-out-synthesis sample

This takes you from a fresh clone to a completed sample run in **well under ten minutes**,
using the **offline** variant — no real Agent CLI, no tokens.

The sample is a hand-written fan-out-synthesis workflow: it fans the **same task** out to two
independent agent branches in parallel, then a `synthesize` node joins **both** branch outputs
into one final answer.

```text
claude_branch ─┐
               ├─→ synthesize
codex_branch  ─┘
```

## 1. Clone and install (≈2 min)

caw needs Python >= 3.12, managed with [uv](https://docs.astral.sh/uv/).

```bash
git clone <this-repo> && cd cli-agentic-workflow
uv sync
```

## 2. Validate the sample, then inspect its graph (≈1 min)

```bash
uv run caw validate examples/fanout-synthesis/fanout-synthesis.mock.yaml
uv run caw graph    examples/fanout-synthesis/fanout-synthesis.mock.yaml
```

`caw graph` prints two independent branches (`claude_branch`, `codex_branch`) fanning into one
`synthesize` node that `needs` both — the fan-out-synthesis shape, before anything runs.

## 3. Run it offline (≈1 min)

```bash
uv run caw run examples/fanout-synthesis/fanout-synthesis.mock.yaml
```

Every node drives the built-in `mock` adapter, which replays a companion fixture
(`claude-branch.fixture.json`, `codex-branch.fixture.json`, `synthesize.fixture.json`), so the
run completes with no real Agent CLI and no tokens. The last line prints
`run <run-id> succeeded` — copy that run id.

## 4. Read the report (≈1 min)

```bash
uv run caw report <run-id> --format markdown
```

The Markdown report keeps the **final conclusion** (each node's outcome, including the
`synthesize` node's) in its own `## Nodes` section, validates the declared `final_output` in
`## Final Output`, and keeps both distinct from the `## Trace` of events — conclusion
separated from trace evidence.

## Going real: claude.print + codex.exec

[`fanout-synthesis.real.yaml`](fanout-synthesis.real.yaml) is the same graph with the two
branches pointed at the real `claude.print` and `codex.exec` adapters, fanning the same task to
both. It spends real tokens and needs **both** the `claude` and `codex` CLIs on PATH and
authenticated, plus two runtime-only inputs the portable file leaves out (an `env` allow-list
and codex's headless `args`) — both explained in the file's header. The e2e
`tests/e2e/test_fanout_synthesis_runs.py` runs this variant end-to-end against both real CLIs
(local-only; CI runs `pytest -m "not e2e"`):

```bash
uv run pytest tests/e2e/test_fanout_synthesis_runs.py -m e2e
```
