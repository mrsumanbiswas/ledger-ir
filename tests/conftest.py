"""Shared fixtures: repo path setup, SPEC.md schema extraction, fixture loaders.

The JSON Schemas live as fenced blocks in SPEC.md (single source of truth);
tests extract them by `$id` and validate fixtures/solver output against them.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "sample_data"
sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return ROOT


@pytest.fixture(scope="session")
def spec_schemas() -> dict[str, dict]:
    text = (ROOT / "SPEC.md").read_text()
    schemas: dict[str, dict] = {}
    for block in re.findall(r"```json\n(.*?)```", text, re.S):
        try:
            obj = json.loads(block)
        except ValueError:
            continue
        if isinstance(obj, dict) and "$id" in obj:
            name = obj["$id"].rsplit("/", 1)[1].removesuffix(".schema.json")
            schemas[name] = obj
    return schemas


@pytest.fixture(scope="session")
def validate(spec_schemas):
    """validate(schema_name, instance) -> raises jsonschema.ValidationError on failure."""
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource

    registry = Registry().with_resources(
        [(s["$id"], Resource.from_contents(s)) for s in spec_schemas.values()])

    def _validate(name: str, instance) -> None:
        Draft202012Validator(spec_schemas[name], registry=registry).validate(instance)

    return _validate


@pytest.fixture()
def gstr2b() -> dict:
    return json.loads((SAMPLE / "gstr2b_oct2026.json").read_text())


@pytest.fixture()
def invoice_tokens() -> dict:
    return json.loads((SAMPLE / "invoice_882_tokens.json").read_text())


@pytest.fixture()
def bank_csv_text() -> str:
    return (SAMPLE / "bank_oct2026.csv").read_text()


@pytest.fixture()
def sample_dir() -> Path:
    return SAMPLE
