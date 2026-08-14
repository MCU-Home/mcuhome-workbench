# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The device version (:mod:`mcuhome.model.ota`).

The model half of the subject; the Matter OTA file the version ends up
in (:mod:`mcuhome.workbench.otafile`) is tested in
``test_ota_workbench.py``.

Two things are worth pinning here and each one is a different kind of
claim:

* the **SemVer to SoftwareVersion mapping** is fixed by ADR 0015
  decision 9 and published to the world in every image's Basic
  Information cluster — changing it silently would make every device in
  the field disagree with every image built after the change;
* a **version out of range is refused with an explanation**, because
  ``256.0.0`` silently becoming ``0.0.0`` on the wire is exactly the
  class of bug the whole one-source-per-group discipline exists to
  prevent.
"""

from __future__ import annotations

import pytest
from conftest import EXAMPLES_DIR, resolve_file

from mcuhome.model import ota
from mcuhome.model.errors import BuildError

EXAMPLE = EXAMPLES_DIR / "00-bmp180-two-endpoints.yaml"


# --------------------------------------------------------------------------
# The version
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("0.0.0", 0x00000000),
        ("0.1.0", 0x00010000),
        ("1.0.0", 0x01000000),
        ("1.2.3", 0x01020300),
        ("255.255.255", 0xFFFFFF00),
    ],
)
def test_the_semver_mapping_is_the_one_the_adr_fixed(version: str, expected: int) -> None:
    """``major << 24 | minor << 16 | patch << 8`` (ADR 0015 decision 9)."""
    assert ota.software_version(version) == expected


def test_the_low_byte_is_reserved_and_therefore_always_zero() -> None:
    """A tweak counter has somewhere to go without re-opening the mapping."""
    for version in ("0.0.1", "9.9.9", "255.255.255"):
        assert ota.software_version(version) & 0xFF == 0


def test_versions_compare_the_way_semver_does() -> None:
    """The only property Matter actually needs from the mapping."""
    ordered = ["0.0.1", "0.1.0", "0.1.1", "0.2.0", "1.0.0", "1.0.1", "2.0.0"]
    numbers = [ota.software_version(item) for item in ordered]
    assert numbers == sorted(numbers)
    assert len(set(numbers)) == len(numbers)


@pytest.mark.parametrize(
    "version",
    ["1.0", "1.0.0.0", "v1.0.0", "1.0.0-rc1", "1.0.0+7", "01.0.0", "", "one.two.three"],
)
def test_a_version_that_is_not_plain_semver_is_refused(version: str) -> None:
    assert ota.describe_version_problem(version) is not None


@pytest.mark.parametrize("version", ["256.0.0", "0.256.0", "0.0.256", "1000.0.0"])
def test_a_field_that_does_not_fit_in_a_byte_is_refused(version: str) -> None:
    """The mapping's own limit, and the message has to say so."""
    problem = ota.describe_version_problem(version)
    assert problem is not None
    assert "one byte each" in problem


def test_parse_version_refuses_with_a_hint() -> None:
    with pytest.raises(BuildError) as error:
        ota.parse_version("1.0")
    assert "device.version" in str(error.value.hint)


def test_the_kconfig_group_states_all_three_symbols_from_one_string() -> None:
    """MCUboot's version and Matter's cannot be allowed to disagree."""
    lines = ota.kconfig_lines("1.2.3", matter=True)
    assert lines == [
        'CONFIG_MCUBOOT_IMGTOOL_SIGN_VERSION="1.2.3"',
        "CONFIG_CHIP_DEVICE_SOFTWARE_VERSION=16909056",
        'CONFIG_CHIP_DEVICE_SOFTWARE_VERSION_STRING="1.2.3"',
    ]


def test_a_device_without_matter_gets_only_the_mcuboot_symbol() -> None:
    assert ota.kconfig_lines("1.2.3", matter=False) == [
        'CONFIG_MCUBOOT_IMGTOOL_SIGN_VERSION="1.2.3"'
    ]


def test_the_resolved_model_carries_the_default_version() -> None:
    model = resolve_file(EXAMPLE)
    assert model.device.version == ota.DEFAULT_VERSION
    assert f'CONFIG_MCUBOOT_IMGTOOL_SIGN_VERSION="{ota.DEFAULT_VERSION}"' in model.build.kconfig
