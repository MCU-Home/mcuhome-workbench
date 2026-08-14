# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The configuration schema, as exported data.

The workbench half of the subject: :mod:`mcuhome.workbench.configschema`
builds the ``main.yaml`` JSON Schema, and it has to describe exactly the
configuration :mod:`mcuhome.workbench.schema`'s parser accepts. Like the
registry next door in ``test_export.py``, the document is a contract with
a consumer that is not in this repository (dashboard ADR 0011): an editor
validates against it. It is therefore golden-tested byte for byte — a
change to it is a change a human approves, not one that happens.
"""

from __future__ import annotations

import json
import re

import pytest
from conftest import GOLDEN_DIR

from mcuhome.model import __version__, export, registry
from mcuhome.workbench import configschema, schema

SCHEMA_GOLDEN = GOLDEN_DIR / "main.schema.json"

#: Placeholder the goldens carry where the builder's own version would
#: be. A golden that pinned the version would have to be regenerated on
#: every release, which would train a reviewer to regenerate goldens.
VERSION_PLACEHOLDER = "0.1.0.dev0"


def _stable(text: str) -> str:
    return text.replace(f'"{__version__}"', f'"{VERSION_PLACEHOLDER}"')


# --------------------------------------------------------------------------
# Golden
# --------------------------------------------------------------------------


def test_the_config_schema_is_what_it_was() -> None:
    assert _stable(export.to_json(configschema.config_json_schema())) == SCHEMA_GOLDEN.read_text(
        "utf-8"
    )


def test_both_documents_are_deterministic() -> None:
    """Same process, same bytes — twice, so nothing leaks in from a set."""
    assert export.to_json(export.registry_data()) == export.to_json(export.registry_data())
    assert export.to_json(configschema.config_json_schema()) == export.to_json(
        configschema.config_json_schema()
    )


# --------------------------------------------------------------------------
# The schema describes the configuration the parser accepts
# --------------------------------------------------------------------------


def test_the_schema_offers_the_boards_the_registry_has() -> None:
    device = configschema.config_json_schema()["properties"]["device"]
    assert device["properties"]["board"]["enum"] == sorted(registry.BOARDS)
    assert device["required"] == ["name", "board"]


def test_the_schema_offers_the_drivers_clusters_and_device_types() -> None:
    document = configschema.config_json_schema()
    peripherals = document["properties"]["hardware"]["properties"]["peripherals"]
    assert peripherals["additionalProperties"]["properties"]["driver"]["enum"] == sorted(
        registry.DRIVERS
    )
    endpoint = document["properties"]["node"]["properties"]["endpoints"]["items"]
    assert endpoint["properties"]["device_type"]["enum"] == sorted(registry.DEVICE_TYPES)
    assert endpoint["properties"]["clusters"]["propertyNames"]["enum"] == sorted(registry.CLUSTERS)


def test_the_schema_rejects_a_section_the_parser_rejects() -> None:
    """`additionalProperties: false` mirrors the parser's reject_unknown."""
    document = configschema.config_json_schema()
    assert document["additionalProperties"] is False
    assert document["properties"]["device"]["additionalProperties"] is False


def test_peripheral_properties_stay_open() -> None:
    """Driver properties are per-driver; only the validator can check them."""
    peripherals = configschema.config_json_schema()["properties"]["hardware"]["properties"][
        "peripherals"
    ]
    assert peripherals["additionalProperties"]["additionalProperties"] is True


@pytest.mark.parametrize(
    ("text", "accepted"),
    [
        ("10s", True),
        ("500ms", True),
        ("5min", True),
        ("1.5h", True),
        ("10", False),
        ("10 seconds", False),
    ],
)
def test_the_duration_pattern_agrees_with_the_parser(text: str, accepted: bool) -> None:
    """An editor that underlines what the builder accepts is a liar."""
    assert bool(re.match(configschema._DURATION_PATTERN, text)) is accepted
    assert bool(schema._DURATION_RE.match(text)) is accepted


@pytest.mark.parametrize(
    ("text", "accepted"),
    [("400kHz", True), ("100KHZ", True), ("1mhz", True), ("400", False), ("400 k", False)],
)
def test_the_frequency_pattern_agrees_with_the_parser(text: str, accepted: bool) -> None:
    assert bool(re.match(configschema._FREQUENCY_PATTERN, text)) is accepted
    assert bool(schema._FREQUENCY_RE.match(text)) is accepted


@pytest.mark.parametrize(("text", "accepted"), [("gpio0.26", True), ("P0.26", False)])
def test_the_pin_pattern_agrees_with_the_parser(text: str, accepted: bool) -> None:
    assert bool(re.match(configschema._PIN_PATTERN, text)) is accepted
    assert bool(schema._PIN_RE.match(text)) is accepted


def test_the_device_name_pattern_is_the_parsers_own() -> None:
    name = configschema.config_json_schema()["properties"]["device"]["properties"]["name"]
    assert name["pattern"] == schema.DEVICE_NAME_RE.pattern
    assert name["maxLength"] == schema.DEVICE_NAME_MAX


def test_the_schema_validates_the_example_when_a_validator_is_installed() -> None:
    """Optional: jsonschema is not a builder dependency, only a check."""
    jsonschema = pytest.importorskip("jsonschema")
    from conftest import EXAMPLES_DIR

    from mcuhome.workbench.loader import load_yaml_file

    document = configschema.config_json_schema()
    jsonschema.Draft202012Validator.check_schema(document)
    data = load_yaml_file(EXAMPLES_DIR / "00-bmp180-two-endpoints.yaml")
    jsonschema.Draft202012Validator(document).validate(json.loads(json.dumps(data, default=str)))
