# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Stage 2a: shape errors, and the error rendering itself."""

from __future__ import annotations

from conftest import VALID_CONFIG, expect_failure, find_error, line_of, resolve_file
from mcuhome.model.errors import ConfigError, Location


def test_unknown_top_level_section(write_config) -> None:
    text = VALID_CONFIG + "sensors:\n  - nothing\n"
    errors = expect_failure(write_config(text))
    error = find_error(errors, 'Unknown key "sensors"')
    assert error.location.line == line_of(text, "sensors:")
    assert error.hint == ("keys allowed here: automations, device, hardware, network, node")


def test_reserved_section_says_which_revision(write_config) -> None:
    text = VALID_CONFIG + "packages:\n  common: shared/base.yaml\n"
    errors = expect_failure(write_config(text))
    error = find_error(errors, '"packages:" is not implemented yet')
    assert error.hint == (
        "packages and includes arrive with schema revision 2; remove the key for now"
    )


def test_missing_device_section(write_config) -> None:
    errors = expect_failure(write_config("network:\n  thread:\n    device_role: ftd\n"))
    assert errors[0].message == 'This configuration has no "device:" section.'
    assert "name: my-sensor" in (errors[0].hint or "")


def test_device_name_must_be_hostname_shaped(write_config) -> None:
    text = VALID_CONFIG.replace("name: bench-node", "name: Bench Node!")
    errors = expect_failure(write_config(text))
    error = find_error(errors, "is not a usable device name")
    assert error.location.line == line_of(text, "name: Bench Node!")
    assert "becomes the node's hostname" in (error.hint or "")


def test_missing_required_key(write_config) -> None:
    text = VALID_CONFIG.replace("  board: nrf7002dk/nrf5340/cpuapp\n", "")
    errors = expect_failure(write_config(text))
    error = find_error(errors, '"board:" is required here but missing')
    assert error.location.key == "device.board"


def test_wrong_type(write_config) -> None:
    text = VALID_CONFIG.replace("name: bench-node", "name: 12")
    errors = expect_failure(write_config(text))
    error = find_error(errors, '"name:" must be text')
    assert "but this is a number" in error.message


def test_value_outside_the_vocabulary(write_config) -> None:
    text = VALID_CONFIG.replace(
        "device:\n  name: bench-node\n",
        "device:\n  name: bench-node\n  blob_usage: sometimes\n",
    )
    errors = expect_failure(write_config(text))
    error = find_error(errors, '"sometimes" is not a valid value for blob_usage:')
    assert error.hint == "use one of: auto, none"


def test_duration_needs_a_unit(write_config) -> None:
    text = VALID_CONFIG.replace("sampling: 10s", "sampling: 10")
    errors = expect_failure(write_config(text))
    error = find_error(errors, '"sampling:" must be a duration with a unit')
    assert error.location.line == line_of(text, "sampling: 10")
    assert error.hint == "units are ms, s, min, h, d — for example sampling: 60s"


def test_unknown_key_inside_a_section(write_config) -> None:
    text = VALID_CONFIG.replace("  matter:\n", "  matter:\n    enable: true\n")
    errors = expect_failure(write_config(text))
    error = find_error(errors, 'Unknown key "enable"')
    assert error.hint == (
        "keys allowed here: discriminator, enabled, passcode, salt, use_test_pairing"
    )


def test_error_rendering_is_stable() -> None:
    error = ConfigError(
        'Board "nrf99dk" is not supported by MCUHome yet.',
        location=Location(file=None, line=5, column=10, key="device.board"),
        hint="use one of the boards MCUHome supports today: nrf7002dk/nrf5340/cpuapp",
    )
    assert error.render() == (
        'Error: Board "nrf99dk" is not supported by MCUHome yet.\n'
        "  in line 5, column 10 (device.board)\n"
        "  Fix: use one of the boards MCUHome supports today: nrf7002dk/nrf5340/cpuapp"
    )


def test_rendering_names_the_file(write_config) -> None:
    entry = write_config(VALID_CONFIG.replace("sampling: 10s", "sampling: 10"))
    errors = expect_failure(entry)
    rendered = errors[0].render(entry.parent)
    assert "in main.yaml, line" in rendered
    assert "Traceback" not in rendered


def test_rendering_without_a_base_names_the_file_absolutely(write_config) -> None:
    """The base directory is the caller's to supply; the module reads no cwd.

    Rendering the same error from a server that happens to stand in the
    config's directory must not silently produce a relative path — the
    shortening is a decision the caller makes, not one the error makes.
    """
    entry = write_config(VALID_CONFIG.replace("sampling: 10s", "sampling: 10"))
    errors = expect_failure(entry)
    assert f"in {entry}, line" in errors[0].render()


def test_an_unquoted_version_says_to_quote_it(write_config) -> None:
    """YAML reads 1.4 as a float; telling a user to learn that is not a message."""
    text = VALID_CONFIG.replace("board:", "version: 1.4\n  board:")
    errors = expect_failure(write_config(text))
    error = find_error(errors, "is not a device version")
    assert error.location.line == line_of(text, "version: 1.4")
    assert 'version: "1.4.0"' in (error.hint or "")


def test_a_quoted_version_is_accepted(write_config) -> None:
    text = VALID_CONFIG.replace("board:", 'version: "2.3.4"\n  board:')
    assert resolve_file(write_config(text)).device.version == "2.3.4"
