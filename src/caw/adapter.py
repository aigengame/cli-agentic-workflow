"""The vendor-neutral Adapter interface and the v0.1 mock Adapter (#5, ADR 0001).

An Adapter is the project-owned integration layer that invokes an Agent CLI and
normalizes its result into the workflow runtime. The kernel only ever speaks to
the abstract :class:`Adapter` and the vendor-neutral :class:`AgentInvocation` /
:class:`AgentResult` data classes, so no Agent-CLI specifics (`claude -p`,
`codex exec`, flag names, output formats) leak into the executor, State, or
Events. Two real Adapters ship behind the interface — :class:`~caw.claude_print.ClaudePrintAdapter`
(#9) and :class:`~caw.codex_exec.CodexExecAdapter` (#11) — alongside the
:class:`MockAdapter` that replays a fixture file offline.
"""

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

# The adapter names the built-in registry resolves with no runtime injection.
# Validation checks an agent Node's `adapter` against this set at normalize time
# so a typo fails `caw validate` fast (#64), before any run directory. Adapters
# injected at run time (a populated AdapterRegistry passed to execute_run) are not
# known at validate time; their unknown-name check stays the run-time registry
# resolve. A new real CLI adds its name here as it lands (#9 claude, #11 codex).
BUILTIN_ADAPTER_NAMES: frozenset[str] = frozenset({"mock", "claude.print", "codex.exec"})


class AdapterError(Exception):
    """Raised when an Adapter cannot produce a normalized result.

    This is a node-level failure (e.g. a missing or malformed fixture), distinct
    from an Agent CLI that ran and exited non-zero — which is a normal non-zero
    :class:`AgentResult`, not an error.
    """


@dataclass(frozen=True, repr=False)
class AgentInvocation:
    """A normalized, vendor-neutral request handed to an Adapter.

    ``env`` carries ONLY the variables the node declared and that were present in
    the parent environment, already resolved to their values by the kernel's env
    policy; an Adapter passes these to the Agent CLI process and nowhere else. The
    kernel never persists these values (#5). ``output_schema`` and ``fixture`` are
    resolved file paths or ``None``. ``working_dir`` is the node-owned directory a
    real Adapter may use as the subprocess cwd and artifact-discovery boundary; it
    is ``None`` for direct adapter calls that deliberately use the ambient cwd.

    The repr REDACTS env values (#65): the declared NAMES render — they are
    already in the inspectable definition — but each VALUE is replaced with a
    redaction marker, so a log line, exception message, or event payload that
    stringifies an invocation cannot leak a secret.
    """

    node_id: str
    adapter: str
    prompt: str
    args: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    output_schema: Path | None = None
    fixture: Path | None = None
    working_dir: Path | None = None

    def __repr__(self) -> str:
        redacted_env = {name: "***" for name in self.env}
        return (
            f"{type(self).__name__}(node_id={self.node_id!r}, adapter={self.adapter!r}, "
            f"prompt={self.prompt!r}, args={self.args!r}, env={redacted_env!r}, "
            f"output_schema={self.output_schema!r}, fixture={self.fixture!r}, "
            f"working_dir={self.working_dir!r})"
        )


@dataclass(frozen=True)
class AgentResult:
    """The normalized result an Adapter returns for one Agent CLI invocation.

    ``structured_output`` is the parsed object the Output Contract validates
    (``None`` when the invocation produced none). ``artifacts`` lists durable
    files the invocation produced, for minimal indexing in State (#5); full
    artifact lifecycle is #16.

    ``adapter_failure`` is the first-class, vendor-neutral signal that the Agent
    CLI RAN but the Adapter normalized its result as a FAILURE — the canonical
    case being Claude's ``is_error: true`` arriving with a zero process exit
    (ADR 0006, #83). It is distinct from ``exit_status``: the Adapter keeps the
    process's REAL exit status in ``exit_status`` (so the trace is honest) and
    raises this flag, instead of manufacturing a fake non-zero exit through the
    exit-code channel. The kernel honors it ONCE — a result that exited zero yet
    carries ``adapter_failure`` is a failed Node — so every real Adapter
    (claude #9, codex #11) signals an agent-determined failure the same way
    rather than re-inventing the convention. A failed node carries no trustworthy
    structured output, so the Adapter drops it and puts the cause on ``stderr``.
    """

    exit_status: int
    stdout: str = ""
    stderr: str = ""
    structured_output: object | None = None
    artifacts: tuple[Path, ...] = ()
    adapter_failure: bool = False


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
    optional ``stdout``, ``stderr``, ``structured_output``, ``artifacts`` (a list
    of file paths), and ``adapter_failure`` (a boolean: the agent ran but the
    Adapter normalizes its result as a FAILURE — the offline analogue of Claude's
    ``is_error: true`` arriving with a zero exit, ADR 0006 / #83). This lets whole
    Workflows and Patterns run with no real Agent CLI installed (#5 acceptance
    criteria 1 and 4).

    The shipped adapter never writes the resolved env to a path: env-observation
    for the env-policy test lives in a test-only adapter seam, so a secret value
    cannot reach a fixture-controlled filesystem sink (#65).
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
        # The read -> json.loads -> dict AdapterError ladder is the SAME one the real
        # subprocess Adapters use, consolidated in caw.subprocess_adapter (#83), so a
        # malformed fixture and a malformed CLI wrapper surface the same shape of
        # error. Imported lazily here to avoid an import cycle (subprocess_adapter
        # imports this module's AdapterError / AgentInvocation).
        from caw.subprocess_adapter import node_context, read_json_object

        raw = read_json_object(fixture, context=node_context(invocation), source_label="fixture")
        exit_status = raw.get("exit_status")
        if not isinstance(exit_status, int) or isinstance(exit_status, bool):
            raise AdapterError(
                f"fixture {fixture} for node {node_id!r} must declare an integer exit_status"
            )
        artifacts = MockAdapter._parse_artifacts(raw.get("artifacts", ()), fixture, node_id)
        # The first-class adapter-determined-failure signal (ADR 0006, #83): an
        # optional boolean that lets an offline fixture model the canonical "the
        # agent ran but its result is a FAILURE" case (Claude's is_error with a zero
        # exit). A non-boolean is a fixture-authoring error, surfaced like the
        # exit_status one; absent, it defaults False (an ordinary result).
        adapter_failure = raw.get("adapter_failure", False)
        if not isinstance(adapter_failure, bool):
            raise AdapterError(
                f"fixture {fixture} for node {node_id!r} 'adapter_failure' must be a boolean"
            )
        return AgentResult(
            exit_status=exit_status,
            stdout=str(raw.get("stdout", "")),
            stderr=str(raw.get("stderr", "")),
            structured_output=raw.get("structured_output"),
            artifacts=artifacts,
            adapter_failure=adapter_failure,
        )

    @staticmethod
    def _parse_artifacts(raw: object, fixture: Path, node_id: str) -> tuple[Path, ...]:
        """Coerce a fixture's ``artifacts`` value into a tuple of paths, or fail cleanly.

        A malformed ``artifacts`` shape (not a list, or an entry that is not a path
        string) is a fixture-authoring error: it must raise a node-level
        :class:`AdapterError` naming the fixture, never a raw ``TypeError`` that
        escapes the Adapter and crashes the Run (#61).
        """
        if not isinstance(raw, list | tuple):
            raise AdapterError(
                f"fixture {fixture} for node {node_id!r} has a malformed 'artifacts': "
                f"expected a list of path strings, got {type(raw).__name__}"
            )
        artifacts: list[Path] = []
        for entry in raw:
            if not isinstance(entry, str):
                raise AdapterError(
                    f"fixture {fixture} for node {node_id!r} has a malformed 'artifacts' "
                    f"entry: expected a path string, got {type(entry).__name__}"
                )
            artifacts.append(Path(entry))
        return tuple(artifacts)


class AdapterRegistry:
    """Resolves an adapter identifier to the Adapter that handles it.

    Decouples the executor's dispatch from concrete Adapter construction: the
    executor looks an Adapter up by the node's ``adapter`` name, so adding a real
    Adapter (#9 claude, #11 codex) is a registry entry, not an executor edit. An
    unknown identifier is a node-level :class:`AdapterError`.
    """

    def __init__(self, adapters: Mapping[str, Adapter] | None = None) -> None:
        self._adapters: dict[str, Adapter] = dict(adapters or _default_adapters())

    def resolve(self, adapter: str) -> Adapter:
        try:
            return self._adapters[adapter]
        except KeyError as exc:
            known = ", ".join(sorted(self._adapters)) or "<none>"
            raise AdapterError(f"unknown adapter {adapter!r} (known: {known})") from exc

    @property
    def names(self) -> frozenset[str]:
        """The adapter identifiers this registry can resolve.

        A resume reconstructs the Workflow from its persisted snapshot and
        re-validates it; the snapshot cannot record which adapters were injected
        at run time, so the resume must re-validate against the registry's own
        known set rather than only the built-ins (#6).
        """
        return frozenset(self._adapters)


def _default_adapters() -> dict[str, Adapter]:
    """The built-in Adapter instances a default registry resolves.

    Mirrors :data:`BUILTIN_ADAPTER_NAMES` so a default-registry run resolves
    every built-in name. Constructing each Adapter has NO side effects — the real
    ``claude.print`` (#9) and ``codex.exec`` (#11) Adapters probe their CLIs lazily
    at invoke / capability-check time — so a shell-only or offline Run never requires
    ``claude`` or ``codex`` to be installed. The real-Adapter imports are deferred to
    break the import cycle (each adapter module imports the interface from here).
    """
    from caw.claude_print import ClaudePrintAdapter
    from caw.codex_exec import CodexExecAdapter

    return {
        "mock": MockAdapter(),
        "claude.print": ClaudePrintAdapter(),
        "codex.exec": CodexExecAdapter(),
    }
