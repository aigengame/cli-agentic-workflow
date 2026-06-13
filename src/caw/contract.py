"""Output Contract loading and validation (ADR 0004, #5).

An Output Contract is the declared schema a Node's normalized output must
satisfy. In v0.1 a Node references one as a file (``output_schema: <path>``) in
JSON Schema draft 2020-12; the kernel loads it and validates the Node's
structured output with the ``jsonschema`` library when the Node completes and
before its dependents run. A violation fails the Node and the error names the
failed contract (its schema path) so a user can see which contract was breached.
"""

import json
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError


class OutputContractError(Exception):
    """Raised when a Node's structured output violates its Output Contract.

    The message always names the failed contract (the ``output_schema`` path) so
    the executor can record a single, self-identifying failure line.
    """


def validate_output_contract(schema_path: Path, structured_output: object) -> None:
    """Validate ``structured_output`` against the JSON Schema at ``schema_path``.

    Raises :class:`OutputContractError` naming the contract on any breach: an
    unreadable or non-JSON schema file, a schema that is itself invalid against
    draft 2020-12, missing structured output, or an instance the schema rejects.
    """
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise OutputContractError(f"cannot read output contract {schema_path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise OutputContractError(f"invalid JSON in output contract {schema_path}: {exc}") from exc

    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        reason = " ".join(str(exc.message).split())
        raise OutputContractError(
            f"output contract {schema_path} is not a valid JSON Schema: {reason}"
        ) from exc

    if structured_output is None:
        raise OutputContractError(
            f"output contract {schema_path} violated: node produced no structured output"
        )

    try:
        Draft202012Validator(schema).validate(structured_output)
    except ValidationError as exc:
        location = exc.json_path or "$"
        reason = " ".join(str(exc.message).split())
        raise OutputContractError(
            f"output contract {schema_path} violated at {location}: {reason}"
        ) from exc
