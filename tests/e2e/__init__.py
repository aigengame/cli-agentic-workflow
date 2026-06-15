"""Real-agent-CLI end-to-end tests (#86).

Tests under this package carry the ``e2e`` marker (applied by location in
``conftest.py``). They exercise behaviors whose correctness depends on a REAL
Agent CLI — a real ``claude -p`` invocation, its result-wrapper shape, and a real
agent Node flowing through ``execute_run`` into State and the Output Contract.

They run LOCALLY ONLY for now (cloud agent auth is not provisionable in CI yet),
and they FAIL — never skip — when the selected agent CLI is unavailable, so a
missing CLI can never read as silent green. The default local ``pytest`` run
includes them; CI runs ``pytest -m "not e2e"``. The shared harness lives in
:mod:`e2e.harness`.
"""
