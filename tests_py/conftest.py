# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures and helpers for the builder tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from mcuhome.cli import load_device_model
from mcuhome.errors import ConfigError, ConfigErrorGroup
from mcuhome.model import DeviceModel
from mcuhome.tree import ConfigTree, find_config_root

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
EXAMPLES_DIR = REPO_ROOT / "docs" / "design" / "examples"
DATA_DIR = TESTS_DIR / "data"
FIXTURE_TREE = DATA_DIR / "tree"
GOLDEN_DIR = DATA_DIR / "golden"

#: A configuration that passes every check, used as the baseline the
#: gate tests break one thing at a time.
VALID_CONFIG = """\
device:
  name: bench-node
  board: nrf7002dk/nrf5340/cpuapp

network:
  thread:
    device_role: ftd
  matter:
    enabled: true

hardware:
  buses:
    i2c0:
      controller: arduino_i2c
  peripherals:
    baro:
      driver: bosch,bmp180
      bus: i2c0

node:
  endpoints:
    - id: 1
      device_type: temperature_sensor
      clusters:
        temperature_measurement:
          source: baro.temperature
          sampling: 10s
"""


def line_of(text: str, needle: str) -> int:
    """1-based line number of the first line containing *needle*."""
    for number, line in enumerate(text.splitlines(), start=1):
        if needle in line:
            return number
    raise AssertionError(f"{needle!r} is not in the configuration")


@pytest.fixture
def write_config(tmp_path: Path):
    """Write a configuration into a throwaway tree and return its path."""

    def write(text: str, *, name: str = "main.yaml", secrets: str | None = None) -> Path:
        path = tmp_path / name
        path.write_text(text, encoding="utf-8")
        if secrets is not None:
            (tmp_path / "secrets.yaml").write_text(secrets, encoding="utf-8")
        return path

    return write


def resolve_file(path: Path) -> DeviceModel:
    """Run stages 1-3 on a configuration file, tree discovery included."""
    root = find_config_root(path.parent)
    tree = ConfigTree(root=root or path.parent, discovered=root is not None)
    return load_device_model(path, tree=tree)


def errors_of(exc: ConfigError | ConfigErrorGroup) -> list[ConfigError]:
    """Flatten a single error or an error group into a list."""
    if isinstance(exc, ConfigErrorGroup):
        return exc.errors
    return [exc]


def expect_failure(path: Path) -> list[ConfigError]:
    """Resolve *path*, expecting it to be rejected, and return the errors."""
    with pytest.raises((ConfigError, ConfigErrorGroup)) as caught:
        resolve_file(path)
    return errors_of(caught.value)


def find_error(errors: list[ConfigError], fragment: str) -> ConfigError:
    """The one error whose message contains *fragment*."""
    matches = [error for error in errors if fragment in error.message]
    assert matches, f"no error mentioning {fragment!r}; got: " + "; ".join(
        error.message for error in errors
    )
    assert len(matches) == 1, f"{fragment!r} matched {len(matches)} errors"
    return matches[0]
