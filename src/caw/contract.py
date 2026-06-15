"""Output Contract loading and validation (ADR 0004, #5).

An Output Contract is the declared schema a Node's normalized output must
satisfy. In v0.1 a Node references one as a file (``output_schema: <path>``) in
JSON Schema draft 2020-12; the kernel loads it and validates the Node's
structured output with the ``jsonschema`` library when the Node completes and
before its dependents run. A violation fails the Node and the error names the
failed contract (its schema path) so a user can see which contract was breached.

The schema file is read, parsed, meta-schema-checked, and compiled into a
validator ONCE per resolved path and cached, so repeated node attempts (and
repeated runs in one process) reuse the compiled validator instead of paying the
read+parse+compile cost every time (#67). The compile is the only blocking I/O on
the path; the executor runs it off the event loop (see
``caw.executor._execute_agent_node``).
"""

import json
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from referencing import Registry
from referencing.exceptions import Unresolvable

# An empty registry with no retrieval callback: a `$ref` the schema does not
# define locally is Unresolvable rather than fetched. This disables the
# deprecated default behavior where jsonschema retrieves a remote `$ref` over the
# network during validation, so an offline Run can neither egress nor stall its
# event loop on a fixture-controlled URL (#61). A remote `$ref` becomes a contract
# error, not network I/O.
_OFFLINE_REGISTRY: Registry = Registry()


class OutputContractError(Exception):
    """Raised when a Node's structured output violates its Output Contract.

    The message always names the failed contract (the ``output_schema`` path) so
    the executor can record a single, self-identifying failure line.
    """


@lru_cache(maxsize=64)
def _compile_validator_cached(resolved: str, _mtime_ns: int) -> Draft202012Validator:
    """Read, parse, meta-schema-check, and compile a validator for a schema file.

    Memoized on the RESOLVED path AND the file's modification stamp, so the
    expensive read+parse+check+construct runs once per (path, contents) and is
    reused across attempts and runs in the same process; two equivalent spellings of
    one file (a ``.`` / ``..`` detour, a symlink) resolve to the same key and share
    a single validator, and rewriting the same path bumps the mtime and recompiles
    rather than serving a stale validator (#67). The ``_mtime_ns`` argument is a
    cache key only — :func:`compile_output_validator` resolves and stats the file
    and passes both.

    ``lru_cache`` is not single-flight: two nodes that cold-miss the SAME schema
    concurrently can each run this compile and one result is discarded. That is
    harmless — the compile is idempotent (read + parse + construct a fresh validator
    from the same file) with no shared mutable state — so a duplicate compile costs a
    little CPU but never affects correctness, and is not worth a lock.
    """
    path = Path(resolved)
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise OutputContractError(f"cannot read output contract {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise OutputContractError(f"invalid JSON in output contract {path}: {exc}") from exc
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        reason = " ".join(str(exc.message).split())
        raise OutputContractError(
            f"output contract {path} is not a valid JSON Schema: {reason}"
        ) from exc
    return Draft202012Validator(schema, registry=_OFFLINE_REGISTRY)


def compile_output_validator(schema_path: Path) -> Draft202012Validator:
    """Return the compiled, cached validator for the schema file at ``schema_path``.

    The read+parse+meta-schema-check+construct happens once per RESOLVED path and
    cached contents (#67); a cache hit returns the SAME validator object, and two
    equivalent spellings of one file share that single validator. A missing or
    malformed schema file raises :class:`OutputContractError` naming the contract,
    exactly as validation does, so the caller handles one error type.
    """
    try:
        # Resolve so equivalent spellings of one file (`.`/`..` detours, symlinks)
        # collapse to a single cache key, matching the documented resolved-path
        # caching. ``resolve()`` also stats the path, so a missing/unstatable file
        # raises here and surfaces the same contract-naming error as the read path.
        resolved = schema_path.resolve(strict=True)
        mtime_ns = resolved.stat().st_mtime_ns
    except OSError as exc:
        # A missing/unstatable file cannot be compiled; surface the same
        # contract-naming error the read path would, before touching the cache.
        raise OutputContractError(f"cannot read output contract {schema_path}: {exc}") from exc
    return _compile_validator_cached(str(resolved), mtime_ns)


def validate_output_contract(schema_path: Path, structured_output: object) -> None:
    """Validate ``structured_output`` against the JSON Schema at ``schema_path``.

    Raises :class:`OutputContractError` naming the contract on any breach: an
    unreadable or non-JSON schema file, a schema that is itself invalid against
    draft 2020-12, missing structured output, or an instance the schema rejects.
    The compiled validator is loaded once per schema path and cached
    (:func:`compile_output_validator`).
    """
    validator = compile_output_validator(schema_path)

    # `structured_output` is validated as-is — including ``None`` (JSON null) —
    # against the declared schema. A schema permitting null (e.g. type
    # [object, null]) passes; a schema requiring content still fails when the
    # output is null. The kernel does not special-case None as an automatic
    # violation, so the schema is the sole arbiter of what the contract allows
    # (#63), and the None-vs-absent distinction is left to the schema author.
    try:
        validator.validate(structured_output)
    except ValidationError as exc:
        location = exc.json_path or "$"
        reason = " ".join(str(exc.message).split())
        raise OutputContractError(
            f"output contract {schema_path} violated at {location}: {reason}"
        ) from exc
    except Unresolvable as exc:
        # A `$ref` the schema does not resolve locally (e.g. a remote URL):
        # remote retrieval is disabled by the offline registry, so this is a
        # contract error naming the unresolved reference, never network I/O.
        reason = " ".join(str(exc).split())
        raise OutputContractError(
            f"output contract {schema_path} has an unresolvable reference: {reason}"
        ) from exc
