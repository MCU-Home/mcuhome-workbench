# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Stage 2b: v0.1 scope gates, cross-references, Matter conformance.

Every assertion here checks the message *text* as well as the location:
these strings are the builder's user interface, so changing one is a
deliberate act that shows up in review (builder-pipeline.md §9).
"""

from __future__ import annotations

from conftest import VALID_CONFIG, expect_failure, find_error, line_of, resolve_file

# --------------------------------------------------------------------------
# v0.1 scope gates
# --------------------------------------------------------------------------


def test_baseline_is_valid(write_config) -> None:
    model = resolve_file(write_config(VALID_CONFIG))
    assert model.device.name == "bench-node"


def test_automations_are_gated(write_config) -> None:
    text = VALID_CONFIG + "automations:\n  - id: warn\n    actions:\n      - log: hi\n"
    errors = expect_failure(write_config(text))
    error = find_error(errors, '"automations:" is not implemented yet')
    assert error.location.line == line_of(text, "automations:")
    assert error.hint == (
        "the automation engine is designed but not built yet — remove the "
        "automations: section for now"
    )


def test_coap_is_gated(write_config) -> None:
    text = VALID_CONFIG.replace("  matter:\n", "  coap:\n    enabled: true\n  matter:\n")
    errors = expect_failure(write_config(text))
    error = find_error(errors, '"coap:" is not implemented yet')
    assert error.location.line == line_of(text, "coap:")
    assert "deferred (ADR 0010)" in (error.hint or "")


def test_wifi_is_gated(write_config) -> None:
    text = VALID_CONFIG.replace(
        "  thread:\n    device_role: ftd\n",
        "  wifi:\n    ssid: home\n    password: secret\n",
    )
    errors = expect_failure(write_config(text))
    error = find_error(errors, "WiFi is not supported yet")
    assert error.location.line == line_of(text, "wifi:")
    assert "device_role: ftd" in (error.hint or "")


def test_sleepy_roles_are_gated(write_config) -> None:
    text = VALID_CONFIG.replace("device_role: ftd", "device_role: sed")
    errors = expect_failure(write_config(text))
    error = find_error(errors, "Sleepy end devices")
    assert error.message == 'Sleepy end devices ("device_role: sed") are not supported yet.'
    assert error.location.line == line_of(text, "device_role: sed")
    assert error.location.key == "network.thread.device_role"
    assert "power-management phase" in (error.hint or "")


def test_poll_interval_on_a_non_sleepy_role(write_config) -> None:
    text = VALID_CONFIG.replace(
        "    device_role: ftd\n", "    device_role: ftd\n    poll_interval: 5s\n"
    )
    errors = expect_failure(write_config(text))
    error = find_error(errors, '"poll_interval:" only applies to sleepy end devices')
    assert error.location.line == line_of(text, "poll_interval: 5s")


def test_actuator_target_is_gated(write_config) -> None:
    text = VALID_CONFIG.replace(
        "          source: baro.temperature\n          sampling: 10s\n",
        "          target: status_led\n",
    )
    errors = expect_failure(write_config(text))
    error = find_error(errors, 'Actuators ("target:" on temperature_measurement)')
    assert error.location.line == line_of(text, "target: status_led")
    assert "write path" in (error.hint or "")


def test_heartbeat_reporting_is_gated(write_config) -> None:
    text = VALID_CONFIG.replace(
        "          sampling: 10s\n",
        "          sampling: 10s\n          report:\n            max_interval: 15min\n",
    )
    errors = expect_failure(write_config(text))
    error = find_error(errors, '"max_interval:" (heartbeat reporting) is not implemented yet')
    assert error.location.line == line_of(text, "max_interval: 15min")


def test_enabling_a_blob_is_refused(write_config) -> None:
    text = VALID_CONFIG.replace(
        "  board: nrf7002dk/nrf5340/cpuapp\n",
        "  board: nrf7002dk/nrf5340/cpuapp\n  blobs:\n    nordic-cc3xx: enabled\n",
    )
    errors = expect_failure(write_config(text))
    error = find_error(errors, "binary blob")
    assert error.message == (
        'MCUHome cannot enable the "nordic-cc3xx" binary blob: no vendor blob is integrated yet.'
    )
    assert error.location.line == line_of(text, "nordic-cc3xx: enabled")
    assert "ADR 0013" in (error.hint or "")


def test_disabling_a_blob_is_accepted(write_config) -> None:
    text = VALID_CONFIG.replace(
        "  board: nrf7002dk/nrf5340/cpuapp\n",
        "  board: nrf7002dk/nrf5340/cpuapp\n  blobs:\n    nordic-cc3xx: disabled\n",
    )
    model = resolve_file(write_config(text))
    assert model.toolchain.blobs == {}


def test_unsupported_zephyr_line_is_refused(write_config) -> None:
    text = VALID_CONFIG.replace(
        "  board: nrf7002dk/nrf5340/cpuapp\n",
        '  board: nrf7002dk/nrf5340/cpuapp\n  zephyr_version: "3.7"\n',
    )
    errors = expect_failure(write_config(text))
    error = find_error(errors, "cannot build against Zephyr 3.7")
    assert "zephyr_version: auto" in (error.hint or "")


def test_zephyr_auto_and_latest_resolve_to_the_supported_line(write_config) -> None:
    for value in ("auto", "latest", '"4.4"', "4.4"):
        text = VALID_CONFIG.replace(
            "  board: nrf7002dk/nrf5340/cpuapp\n",
            f"  board: nrf7002dk/nrf5340/cpuapp\n  zephyr_version: {value}\n",
        )
        model = resolve_file(write_config(text))
        assert model.toolchain.zephyr_line == "4.4"


def test_bus_pin_assignment_is_gated(write_config) -> None:
    text = VALID_CONFIG.replace(
        "      controller: arduino_i2c\n",
        "      sda: gpio1.11\n      scl: gpio1.12\n",
    )
    errors = expect_failure(write_config(text))
    error = find_error(errors, "Assigning bus pins")
    assert error.location.line == line_of(text, "sda: gpio1.11")
    assert "controller: arduino_i2c" in (error.hint or "")


# --------------------------------------------------------------------------
# Platform capability
# --------------------------------------------------------------------------


def test_unsupported_board(write_config) -> None:
    text = VALID_CONFIG.replace(
        "board: nrf7002dk/nrf5340/cpuapp", "board: nrf54l15dk/nrf54l15/cpuapp"
    )
    errors = expect_failure(write_config(text))
    error = find_error(errors, "does not support the board")
    assert error.location.key == "device.board"
    assert "nrf7002dk/nrf5340/cpuapp" in (error.hint or "")


def test_unknown_board(write_config) -> None:
    text = VALID_CONFIG.replace("board: nrf7002dk/nrf5340/cpuapp", "board: my-own-board")
    errors = expect_failure(write_config(text))
    error = find_error(errors, "is not a board MCUHome knows")
    assert "Zephyr board target string, verbatim" in (error.hint or "")


def test_unsupported_driver_names_what_works(write_config) -> None:
    text = VALID_CONFIG.replace("driver: bosch,bmp180", "driver: sensirion,sht4x")
    errors = expect_failure(write_config(text))
    error = find_error(errors, 'cannot build "sensirion,sht4x" peripherals yet')
    assert "supported today: bosch,bmp180" in (error.hint or "")


def test_unknown_driver(write_config) -> None:
    text = VALID_CONFIG.replace("driver: bosch,bmp180", "driver: acme,widget")
    errors = expect_failure(write_config(text))
    error = find_error(errors, "is not a driver MCUHome knows")
    assert "compatible string" in (error.hint or "")


def test_unknown_driver_property(write_config) -> None:
    text = VALID_CONFIG.replace("      bus: i2c0\n", "      bus: i2c0\n      oversampling: 3\n")
    errors = expect_failure(write_config(text))
    error = find_error(errors, 'has no setting called "oversampling"')
    assert error.hint == "settings this driver accepts: osr-press"


def test_fixed_address_cannot_be_changed(write_config) -> None:
    text = VALID_CONFIG.replace("      bus: i2c0\n", "      bus: i2c0\n      address: 0x76\n")
    errors = expect_failure(write_config(text))
    error = find_error(errors, "cannot be changed")
    assert "answers to 0x77 in silicon" in (error.hint or "")


# --------------------------------------------------------------------------
# Cross-references
# --------------------------------------------------------------------------


def test_unknown_bus_reference(write_config) -> None:
    text = VALID_CONFIG.replace("      bus: i2c0\n", "      bus: i2c9\n")
    errors = expect_failure(write_config(text))
    error = find_error(errors, 'is on a bus called "i2c9", which does not exist')
    assert error.hint == "buses defined in this configuration: i2c0"


def test_unknown_source_peripheral(write_config) -> None:
    text = VALID_CONFIG.replace("source: baro.temperature", "source: other.temperature")
    errors = expect_failure(write_config(text))
    error = find_error(errors, 'There is no peripheral called "other"')
    assert error.hint == "peripherals in this configuration: baro"


def test_unknown_source_channel(write_config) -> None:
    text = VALID_CONFIG.replace("source: baro.temperature", "source: baro.humidity")
    errors = expect_failure(write_config(text))
    error = find_error(errors, 'has no reading called "humidity"')
    assert error.hint == "bosch,bmp180 provides: pressure, temperature"


def test_source_without_a_channel(write_config) -> None:
    text = VALID_CONFIG.replace("source: baro.temperature", "source: baro")
    errors = expect_failure(write_config(text))
    error = find_error(errors, "does not name a peripheral reading")
    assert "<peripheral>.<channel>" in (error.hint or "")


def test_wrong_quantity_is_refused(write_config) -> None:
    text = VALID_CONFIG.replace("source: baro.temperature", "source: baro.pressure")
    errors = expect_failure(write_config(text))
    error = find_error(errors, "measures pressure")
    assert error.message == (
        '"baro.pressure" measures pressure, but the temperature_measurement cluster '
        "reports temperature."
    )


def test_duplicate_endpoint_ids(write_config) -> None:
    text = (
        VALID_CONFIG
        + """\
    - id: 1
      device_type: pressure_sensor
      clusters:
        pressure_measurement:
          source: baro.pressure
"""
    )
    errors = expect_failure(write_config(text))
    error = find_error(errors, "Endpoint 1 is defined twice")
    assert "every endpoint needs its own id" in (error.hint or "")


def test_duplicate_alias(write_config) -> None:
    text = (
        VALID_CONFIG.replace("    - id: 1\n", "    - id: 1\n      alias: temperature\n")
        + """\
    - id: 2
      alias: temperature
      device_type: pressure_sensor
      clusters:
        pressure_measurement:
          source: baro.pressure
"""
    )
    errors = expect_failure(write_config(text))
    error = find_error(errors, 'The alias "temperature" is used twice')
    assert error.location.key == "node.endpoints[1].alias"


# --------------------------------------------------------------------------
# Matter conformance
# --------------------------------------------------------------------------


def test_missing_mandatory_cluster(write_config) -> None:
    text = VALID_CONFIG.replace("temperature_measurement", "pressure_measurement").replace(
        "source: baro.temperature", "source: baro.pressure"
    )
    errors = expect_failure(write_config(text))
    error = find_error(errors, 'must have the "temperature_measurement" cluster')
    assert "Matter requires it for this device type" in (error.hint or "")


def test_unsupported_device_type(write_config) -> None:
    text = VALID_CONFIG.replace("device_type: temperature_sensor", "device_type: humidity_sensor")
    errors = expect_failure(write_config(text))
    error = find_error(errors, 'cannot build "humidity_sensor" endpoints yet')
    assert "device types supported today: pressure_sensor, temperature_sensor" in (error.hint or "")


def test_cluster_without_a_source(write_config) -> None:
    text = VALID_CONFIG.replace("          source: baro.temperature\n", "")
    errors = expect_failure(write_config(text))
    error = find_error(errors, "has no source")
    assert "source: <peripheral>.temperature" in (error.hint or "")


def test_endpoint_without_clusters(write_config) -> None:
    text = VALID_CONFIG.replace(
        """      clusters:
        temperature_measurement:
          source: baro.temperature
          sampling: 10s
""",
        "",
    )
    errors = expect_failure(write_config(text))
    find_error(errors, "Endpoint 1 has no clusters")


def test_range_outside_the_attribute(write_config) -> None:
    text = VALID_CONFIG.replace(
        "          sampling: 10s\n",
        "          sampling: 10s\n          range:\n            min: -40\n            max: 400\n",
    )
    errors = expect_failure(write_config(text))
    error = find_error(errors, "is outside what the temperature_measurement cluster can report")
    assert error.location.line == line_of(text, "max: 400")
    assert error.hint == "the attribute holds -327.68 to 327.67 °C"


def test_delta_finer_than_the_attribute(write_config) -> None:
    text = VALID_CONFIG.replace(
        "          sampling: 10s\n",
        "          sampling: 10s\n          report:\n            delta: 0.001\n",
    )
    errors = expect_failure(write_config(text))
    error = find_error(errors, "is finer than the temperature_measurement cluster can report")
    assert "smallest step this attribute carries is 0.01 °C" in (error.hint or "")


def test_endpoints_without_matter(write_config) -> None:
    text = VALID_CONFIG.replace("    enabled: true", "    enabled: false")
    errors = expect_failure(write_config(text))
    error = find_error(errors, "Matter is switched off")
    assert "CoAP is deferred" in (error.hint or "")


# --------------------------------------------------------------------------
# network.matter: commissioning credentials
# --------------------------------------------------------------------------

TEST_PAIRING_LINE = "    use_test_pairing: true\n"


def _with_credentials(**values: object) -> str:
    """VALID_CONFIG with explicit credentials instead of the test opt-in."""
    lines = "".join(f"    {key}: {value}\n" for key, value in values.items())
    return VALID_CONFIG.replace(TEST_PAIRING_LINE, lines)


CREDENTIALS = {
    "discriminator": 2314,
    "passcode": 84920174,
    "salt": '"GBG/P9QwOwhgSLc1fnDR7FWutk+sSsOutub53NjXsp8="',
}


def test_a_configuration_with_its_own_credentials_is_valid(write_config) -> None:
    model = resolve_file(write_config(_with_credentials(**CREDENTIALS)))
    assert model.network.pairing is not None
    assert model.network.pairing.passcode == 84920174
    assert not model.network.pairing.test_credentials


def test_a_matter_device_without_credentials_is_refused(write_config) -> None:
    text = VALID_CONFIG.replace(TEST_PAIRING_LINE, "")
    errors = expect_failure(write_config(text))
    error = find_error(errors, "no commissioning credentials")
    assert error.message == "This device has no commissioning credentials."
    assert error.location.line == line_of(text, "  matter:")
    assert "mcuhome init-pairing bench-node" in (error.hint or "")
    assert "use_test_pairing: true" in (error.hint or "")


def test_half_written_credentials_name_what_is_missing(write_config) -> None:
    text = _with_credentials(discriminator=2314, passcode=84920174)
    errors = expect_failure(write_config(text))
    error = find_error(errors, "incomplete")
    assert error.message == "The commissioning credentials are incomplete: salt: missing."
    assert error.location.line == line_of(text, "discriminator:")
    assert "mcuhome init-pairing bench-node --force" in (error.hint or "")


def test_a_passcode_out_of_range_is_refused(write_config) -> None:
    text = _with_credentials(**{**CREDENTIALS, "passcode": 99999999999})
    errors = expect_failure(write_config(text))
    error = find_error(errors, "is not a passcode Matter can carry")
    assert error.message == '"passcode: 99999999999" is not a passcode Matter can carry.'
    assert error.location.line == line_of(text, "passcode:")
    assert "a passcode is a number from 1 to 99999998" in (error.hint or "")


def test_a_forbidden_passcode_is_refused(write_config) -> None:
    text = _with_credentials(**{**CREDENTIALS, "passcode": 12345678})
    errors = expect_failure(write_config(text))
    error = find_error(errors, "one of the passcodes Matter forbids")
    assert error.message == '"passcode: 12345678" is one of the passcodes Matter forbids.'
    assert error.location.line == line_of(text, "passcode:")
    assert "the first thing anyone tries" in (error.hint or "")


def test_a_discriminator_out_of_range_is_refused(write_config) -> None:
    text = _with_credentials(**{**CREDENTIALS, "discriminator": 5000})
    errors = expect_failure(write_config(text))
    error = find_error(errors, "outside the range Matter allows")
    assert error.message == '"discriminator: 5000" is outside the range Matter allows.'
    assert error.location.line == line_of(text, "discriminator:")
    assert "a number from 0 to 4095 (0x000 to 0xFFF)" in (error.hint or "")


def test_a_salt_that_is_not_base64_is_refused(write_config) -> None:
    text = _with_credentials(**{**CREDENTIALS, "salt": '"not a salt!"'})
    errors = expect_failure(write_config(text))
    error = find_error(errors, "not a usable commissioning salt")
    assert error.message == '"salt:" is not a usable commissioning salt: it is not base64 text.'
    assert error.location.line == line_of(text, "salt:")


def test_a_salt_of_the_wrong_length_says_how_long_it_is(write_config) -> None:
    text = _with_credentials(**{**CREDENTIALS, "salt": '"dG9vX3Nob3J0"'})
    errors = expect_failure(write_config(text))
    error = find_error(errors, "not a usable commissioning salt")
    assert error.message == (
        '"salt:" is not a usable commissioning salt: this one is 9 bytes once decoded.'
    )
    assert "16 to 32 random bytes" in (error.hint or "")


def test_asking_for_both_kinds_of_credentials_is_refused(write_config) -> None:
    text = VALID_CONFIG.replace(
        TEST_PAIRING_LINE,
        TEST_PAIRING_LINE + "".join(f"    {key}: {value}\n" for key, value in CREDENTIALS.items()),
    )
    errors = expect_failure(write_config(text))
    error = find_error(errors, "at the same time")
    assert error.location.line == line_of(text, "use_test_pairing:")
    assert "discriminator:, passcode:, salt:" in (error.hint or "")


def test_credentials_on_a_device_without_matter_are_refused(write_config) -> None:
    text = _with_credentials(**CREDENTIALS).replace("    enabled: true", "    enabled: false")
    errors = expect_failure(write_config(text))
    error = find_error(errors, '"passcode:" only means something')
    assert error.location.line == line_of(text, "passcode:")
    assert "switch Matter on" in (error.hint or "")


def test_the_test_credentials_are_resolved_as_chips_own(write_config) -> None:
    model = resolve_file(write_config(VALID_CONFIG))
    assert model.network.pairing is not None
    assert model.network.pairing.test_credentials
    assert model.network.pairing.passcode == 20202021
    assert model.network.pairing.discriminator == 0xF00


def test_every_problem_is_reported_at_once(write_config) -> None:
    text = VALID_CONFIG.replace("device_role: ftd", "device_role: sed").replace(
        "driver: bosch,bmp180", "driver: sensirion,sht4x"
    )
    errors = expect_failure(write_config(text))
    messages = [error.message for error in errors]
    assert any("Sleepy end devices" in message for message in messages)
    assert any("sensirion,sht4x" in message for message in messages)
    assert [error.location.line for error in errors] == sorted(
        error.location.line for error in errors
    )


# --------------------------------------------------------------------------
# device.version
# --------------------------------------------------------------------------


def test_a_version_field_too_large_for_the_matter_mapping_is_refused(write_config) -> None:
    """ADR 0015 decision 9 packs each field into one byte.

    Without this check 256.0.0 would silently become 0.0.0 on the wire —
    a device reporting a version it was not built as, to a controller that
    then refuses every genuinely newer image.
    """
    text = VALID_CONFIG.replace("board:", 'version: "256.0.0"\n  board:')
    errors = expect_failure(write_config(text))
    error = find_error(errors, "is not a usable device version")
    assert error.location.line == line_of(text, 'version: "256.0.0"')
    assert "one byte each" in (error.hint or "")
    assert "MCUboot's image version" in (error.hint or "")


def test_a_prerelease_version_is_refused(write_config) -> None:
    """Matter's SoftwareVersion is a number; there is nowhere to put -rc1."""
    text = VALID_CONFIG.replace("board:", 'version: "1.0.0-rc1"\n  board:')
    errors = expect_failure(write_config(text))
    assert find_error(errors, "is not a usable device version")


def test_the_version_reaches_the_kconfig_fragment(write_config) -> None:
    text = VALID_CONFIG.replace("board:", 'version: "1.2.3"\n  board:')
    model = resolve_file(write_config(text))
    assert 'CONFIG_MCUBOOT_IMGTOOL_SIGN_VERSION="1.2.3"' in model.build.kconfig
    assert "CONFIG_CHIP_DEVICE_SOFTWARE_VERSION=16909056" in model.build.kconfig
