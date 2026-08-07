# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The design examples in ``docs/design/examples/``.

Example 0 is the hardware-verified device and must validate. Examples
1-3 are *design targets*: they describe the finished schema, and the
builder must reject them today — with the specific "not supported yet"
message for the specific feature each one is ahead of, never with a
generic failure.
"""

from __future__ import annotations

import pytest
from conftest import EXAMPLES_DIR, expect_failure, find_error, resolve_file


def test_example_00_is_valid() -> None:
    model = resolve_file(EXAMPLES_DIR / "00-bmp180-two-endpoints.yaml")
    assert model.device.name == "bmp180-node"
    assert [endpoint.id for endpoint in model.endpoints] == [1, 2]


@pytest.mark.parametrize(
    ("example", "expected"),
    [
        # Thread Sleepy End Device, plus a humidity endpoint.
        (
            "01-climate-sensor-sed.yaml",
            [
                'Sleepy end devices ("device_role: sed") are not supported yet.',
                'MCUHome cannot build "humidity_sensor" endpoints yet.',
                'MCUHome cannot build the "relative_humidity_measurement" cluster yet.',
                '"max_interval:" (heartbeat reporting) is not implemented yet.',
            ],
        ),
        # Four endpoints, three unsupported drivers, battery monitoring.
        (
            "02-multi-sensor-station.yaml",
            [
                'MCUHome cannot build "bosch,bme680" peripherals yet.',
                'MCUHome cannot build "rohm,bh1750" peripherals yet.',
                'MCUHome cannot build "voltage-divider" peripherals yet.',
                'MCUHome cannot build "light_sensor" endpoints yet.',
            ],
        ),
        # Automations, actuators and a synchronized sleepy end device.
        (
            "03-co2-alarm-automation.yaml",
            [
                '"automations:" is not implemented yet.',
                'Sleepy end devices ("device_role: ssed") are not supported yet.',
                'Actuators ("target:" on on_off) are not supported yet.',
                'MCUHome cannot build the "on_off" cluster yet.',
                'MCUHome cannot build "gpio-led" peripherals yet.',
            ],
        ),
    ],
)
def test_design_examples_are_rejected_precisely(example: str, expected: list[str]) -> None:
    errors = expect_failure(EXAMPLES_DIR / example)
    messages = [error.message for error in errors]
    for message in expected:
        assert message in messages, f"{example}: missing {message!r}, got {messages}"
    for error in errors:
        assert error.location.file is not None
        assert error.location.line is not None
        assert error.hint, f"{example}: {error.message} has no fix hint"


def test_examples_that_use_an_unsupported_board_say_which_ones_work() -> None:
    errors = expect_failure(EXAMPLES_DIR / "01-climate-sensor-sed.yaml")
    error = find_error(errors, "does not support the board")
    assert "nrf7002dk/nrf5340/cpuapp" in (error.hint or "")
