"""Output Contract validation unit tests (#5, ADR 0004).

These exercise `validate_output_contract` directly: it loads a JSON Schema
draft 2020-12 file and validates a Node's structured output, raising
`OutputContractError` naming the failed contract on any breach.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from caw.contract import (
    OutputContractError,
    compile_output_validator,
    validate_output_contract,
)


def write_schema(path: Path, schema: dict[str, Any]) -> Path:
    path.write_text(json.dumps(schema), encoding="utf-8")
    return path


def test_the_compiled_validator_is_cached_and_reused_across_calls(tmp_path: Path) -> None:
    # #67: the schema file is read, parsed, meta-schema-checked, and compiled into a
    # validator ONCE per resolved path, then reused across node attempts — not
    # re-read and re-compiled on every call. Object identity proves the reuse.
    schema = write_schema(tmp_path / "s.json", {"type": "object", "required": ["x"]})

    first = compile_output_validator(schema)
    second = compile_output_validator(schema)

    assert first is second, "the compiled validator is cached and reused for the same path"


def test_a_changed_schema_file_is_recompiled_not_served_stale(tmp_path: Path) -> None:
    # The cache is keyed by path AND the file's modification stamp, so rewriting the
    # same path (e.g. across runs in one process) recompiles rather than serving a
    # stale validator. A definition that no longer requires `x` must accept `{}`.
    schema = tmp_path / "s.json"
    write_schema(schema, {"type": "object", "required": ["x"]})
    first = compile_output_validator(schema)

    write_schema(schema, {"type": "object"})
    second = compile_output_validator(schema)

    assert first is not second, "a changed schema file is recompiled, not served stale"
    validate_output_contract(schema, {})  # the relaxed schema now accepts an empty object


def test_valid_instance_passes_silently(tmp_path: Path) -> None:
    schema = write_schema(tmp_path / "s.json", {"type": "object", "required": ["x"]})
    validate_output_contract(schema, {"x": 1})


def test_violation_names_the_contract_path_and_the_failing_location(tmp_path: Path) -> None:
    schema = write_schema(
        tmp_path / "s.json",
        {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]},
    )

    with pytest.raises(OutputContractError) as excinfo:
        validate_output_contract(schema, {"x": 123})

    message = str(excinfo.value)
    assert str(schema) in message, "the error names the failed contract"
    assert "violated" in message


def test_missing_structured_output_violates_the_contract(tmp_path: Path) -> None:
    schema = write_schema(tmp_path / "s.json", {"type": "object"})

    with pytest.raises(OutputContractError) as excinfo:
        validate_output_contract(schema, None)

    assert str(schema) in str(excinfo.value)


def test_a_schema_that_is_not_valid_json_schema_is_reported_as_such(tmp_path: Path) -> None:
    # `type` must be a string or array of strings; an integer makes the schema
    # itself invalid against draft 2020-12.
    schema = write_schema(tmp_path / "s.json", {"type": 42})

    with pytest.raises(OutputContractError) as excinfo:
        validate_output_contract(schema, {"anything": True})

    message = str(excinfo.value)
    assert str(schema) in message
    assert "not a valid JSON Schema" in message


def test_a_non_json_schema_file_is_reported_naming_the_contract(tmp_path: Path) -> None:
    schema = tmp_path / "s.json"
    schema.write_text("this is not json", encoding="utf-8")

    with pytest.raises(OutputContractError) as excinfo:
        validate_output_contract(schema, {"x": 1})

    assert str(schema) in str(excinfo.value)


def test_a_missing_schema_file_is_reported_naming_the_contract(tmp_path: Path) -> None:
    schema = tmp_path / "absent.json"

    with pytest.raises(OutputContractError) as excinfo:
        validate_output_contract(schema, {"x": 1})

    assert str(schema) in str(excinfo.value)


def test_a_schema_permitting_null_accepts_null_structured_output(tmp_path: Path) -> None:
    # #63: a schema that legitimately permits null (e.g. type [object, null]) and a
    # null structured output must PASS — null is validated against the schema
    # rather than special-cased as an automatic violation.
    schema = write_schema(tmp_path / "s.json", {"type": ["object", "null"]})

    validate_output_contract(schema, None)


def test_a_schema_requiring_content_still_fails_on_null(tmp_path: Path) -> None:
    # #63: a schema that requires content (here, an object) and a null structured
    # output must still FAIL, naming the contract.
    schema = write_schema(tmp_path / "s.json", {"type": "object", "required": ["summary"]})

    with pytest.raises(OutputContractError) as excinfo:
        validate_output_contract(schema, None)

    assert str(schema) in str(excinfo.value), "the error names the failed contract"


def test_a_remote_ref_schema_fails_as_a_contract_error_without_network(tmp_path: Path) -> None:
    # A schema with a remote `$ref` must fail the contract (naming it) and must NOT
    # attempt to retrieve the remote resource over the network: an otherwise-offline
    # run cannot egress or stall its event loop on a fixture-controlled URL (#61).
    schema = write_schema(tmp_path / "s.json", {"$ref": "https://example.com/remote.json"})

    with pytest.raises(OutputContractError) as excinfo:
        validate_output_contract(schema, {"x": 1})

    message = str(excinfo.value)
    assert str(schema) in message, "the error names the failed contract"
