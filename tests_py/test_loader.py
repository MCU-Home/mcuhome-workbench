# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Stage 1: YAML parsing and ``!secret`` resolution."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import line_of
from mcuhome.model.errors import ConfigError

from mcuhome.workbench.loader import load_config, load_yaml_file

CONFIG_WITH_SECRET = """\
device:
  name: bench-node
  friendly_name: !secret device_label
  board: nrf7002dk/nrf5340/cpuapp
"""


def test_secret_is_resolved(write_config) -> None:
    entry = write_config(CONFIG_WITH_SECRET, secrets="device_label: Bench Node\n")
    data = load_config(entry, secrets_file=entry.parent / "secrets" / "main.yaml")
    assert data["device"]["friendly_name"] == "Bench Node"


def test_unknown_secret_points_at_the_tag(write_config) -> None:
    entry = write_config(CONFIG_WITH_SECRET, secrets="other_label: Nope\n")
    with pytest.raises(ConfigError) as caught:
        load_config(entry, secrets_file=entry.parent / "secrets" / "main.yaml")

    error = caught.value
    assert error.message == 'There is no secret called "device_label" in main.yaml.'
    assert error.location.line == line_of(CONFIG_WITH_SECRET, "!secret")
    assert error.location.column == CONFIG_WITH_SECRET.splitlines()[2].index("!secret") + 1
    assert error.location.key == "device.friendly_name"
    assert "device_label: your-value-here" in (error.hint or "")
    assert "secrets currently defined: other_label" in (error.hint or "")


def test_missing_secrets_file_is_explained(write_config) -> None:
    entry = write_config(CONFIG_WITH_SECRET)
    with pytest.raises(ConfigError) as caught:
        load_config(entry, secrets_file=entry.parent / "secrets" / "main.yaml")
    assert "there is no main.yaml to read it from" in caught.value.message
    assert caught.value.location.line == line_of(CONFIG_WITH_SECRET, "!secret")


def test_secrets_file_is_only_read_when_used(write_config) -> None:
    entry = write_config("device:\n  name: bench-node\n")
    # No secrets/main.yaml exists, and none is needed.
    data = load_config(entry, secrets_file=entry.parent / "secrets" / "main.yaml")
    assert data["device"]["name"] == "bench-node"


def test_broken_yaml_reports_a_line(write_config) -> None:
    entry = write_config("device:\n  name: bench\n   board: oops\n")
    with pytest.raises(ConfigError) as caught:
        load_yaml_file(entry)
    assert caught.value.message.startswith("This file is not valid YAML")
    assert caught.value.location.line is not None
    assert "indentation-sensitive" in (caught.value.hint or "")


def test_empty_configuration_is_rejected(write_config) -> None:
    entry = write_config("# nothing here\n")
    with pytest.raises(ConfigError) as caught:
        load_config(entry, secrets_file=entry.parent / "secrets" / "main.yaml")
    assert caught.value.message == "This device configuration is empty."


def test_missing_file_is_reported(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as caught:
        load_yaml_file(tmp_path / "gone.yaml")
    assert "does not exist" in caught.value.message
