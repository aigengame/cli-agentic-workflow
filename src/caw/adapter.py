"""The vendor-neutral Adapter interface and the v0.1 mock Adapter (#5, ADR 0001).

An Adapter is the project-owned integration layer that invokes an Agent CLI and
normalizes its result into the workflow runtime. The kernel only ever speaks to
the abstract :class:`Adapter` and the vendor-neutral :class:`AgentInvocation` /
:class:`AgentResult` data classes, so no Agent-CLI specifics (`claude -p`,
`codex exec`, flag names, output formats) leak into the executor, State, or
Events. Real Adapters land in later issues (#9 claude, #11 codex); v0.1 ships
the interface plus one :class:`MockAdapter` that replays a fixture file offline.
"""

import json
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path


class AdapterError(Exception):
    """Raised when an Adapter cannot produce a normalized result.

    This is a node-level failure (e.g. a missing or malformed fixture), distinct
    from an Agent CLI that ran and exited non-zero — which is a normal non-zero
    :class:`AgentResult`, not an error.
    """


@dataclass(frozen=True)
class AgentInvocation:
    """A normalized, vendor-neutral request handed to an Adapter.

    ``env`` carries ONLY the variables the node declared and that were present in
    the parent environment, already resolved to their values by the kernel's env
    policy; an Adapter passes these to the Agent CLI process and nowhere else. The
    kernel never persists these values (#5). ``output_schema`` and ``fixture`` are
    resolved file paths or ``None``.
    """

    node_id: str
    adapter: str
    prompt: str
    args: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    output_schema: Path | None = None
    fixture: Path | None = None


@dataclass(frozen=True)
class AgentResult:
    """The normalized result an Adapter returns for one Agent CLI invocation.

    ``structured_output`` is the parsed object the Output Contract validates
    (``None`` when the invocation produced none). ``artifacts`` lists durable
    files the invocation produced, for minimal indexing in State (#5); full
    artifact lifecycle is #16.
    """

    exit_status: int
    stdout: str = ""
    stderr: str = ""
    structured_output: object | None = None
    artifacts: tuple[Path, ...] = ()


class Adapter(ABC):
    """Invokes an Agent CLI and normalizes its result into the workflow runtime."""

    @abstractmethod
    async def invoke(self, invocation: AgentInvocation) -> AgentResult:
        """Run the Agent CLI for ``invocation`` and return a normalized result."""
        raise NotImplementedError


class MockAdapter(Adapter):
    """An offline Adapter that replays a fixture file as a normalized result.

    The fixture is the canned normalized result for an agent Node, located by the
    node's ``fixture`` path. It is a JSON object with an ``exit_status`` and
    optional ``stdout``, ``stderr``, ``structured_output``, and ``artifacts``
    (a list of file paths). This lets whole Workflows and Patterns run with no
    real Agent CLI installed (#5 acceptance criteria 1 and 4).

    For env-policy testing the fixture may also set ``echo_env_to``: a file path
    the mock writes the env it received to. This stands in for the environment a
    real Agent CLI process would observe, so a test can assert that ONLY declared
    variables — already filtered by the kernel's env policy — reached the Node.
    """

    async def invoke(self, invocation: AgentInvocation) -> AgentResult:
        if invocation.fixture is None:
            raise AdapterError(f"mock adapter requires a fixture for node {invocation.node_id!r}")
        return self._replay(invocation)

    @staticmethod
    def _replay(invocation: AgentInvocation) -> AgentResult:
        fixture = invocation.fixture
        assert fixture is not None  # guarded by invoke
        node_id = invocation.node_id
        try:
            raw = json.loads(fixture.read_text(encoding="utf-8"))
        except OSError as exc:
            raise AdapterError(
                f"cannot read fixture {fixture} for node {node_id!r}: {exc}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise AdapterError(
                f"invalid JSON fixture {fixture} for node {node_id!r}: {exc}"
            ) from exc
        if not isinstance(raw, dict):
            raise AdapterError(f"fixture {fixture} for node {node_id!r} must be a JSON object")
        exit_status = raw.get("exit_status")
        if not isinstance(exit_status, int) or isinstance(exit_status, bool):
            raise AdapterError(
                f"fixture {fixture} for node {node_id!r} must declare an integer exit_status"
            )
        echo_env_to = raw.get("echo_env_to")
        if isinstance(echo_env_to, str):
            Path(echo_env_to).write_text(json.dumps(dict(invocation.env)), encoding="utf-8")
        artifacts = tuple(Path(entry) for entry in raw.get("artifacts", ()))
        return AgentResult(
            exit_status=exit_status,
            stdout=str(raw.get("stdout", "")),
            stderr=str(raw.get("stderr", "")),
            structured_output=raw.get("structured_output"),
            artifacts=artifacts,
        )


class AdapterRegistry:
    """Resolves an adapter identifier to the Adapter that handles it.

    Decouples the executor's dispatch from concrete Adapter construction: the
    executor looks an Adapter up by the node's ``adapter`` name, so adding the
    real claude/codex Adapters (#9, #11) is a registry entry, not an executor
    edit. An unknown identifier is a node-level :class:`AdapterError`.
    """

    def __init__(self, adapters: Mapping[str, Adapter] | None = None) -> None:
        self._adapters: dict[str, Adapter] = dict(adapters or {"mock": MockAdapter()})

    def resolve(self, adapter: str) -> Adapter:
        try:
            return self._adapters[adapter]
        except KeyError as exc:
            known = ", ".join(sorted(self._adapters)) or "<none>"
            raise AdapterError(f"unknown adapter {adapter!r} (known: {known})") from exc
