# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Pipeline stage 2: cross-references, capabilities, Matter conformance.

Shape is already done (:mod:`mcuhome.schema`). What remains are the
checks that need knowledge:

1. **v0.1 scope gates.** The schema design describes the finished
   product; this builder implements a slice of it. Everything designed
   but not built is rejected with "not implemented yet" and a location —
   never silently ignored (yaml-schema.md §4, §8).
2. **Cross-references.** A peripheral's bus, a cluster's source channel,
   endpoint IDs and aliases.
3. **Platform capability.** Board support and radio availability.
4. **Matter conformance.** A device type's mandatory clusters, and
   whether the requested measurement fits the attribute the cluster
   defines.
5. **Commissioning credentials.** That the device has any at all, and
   that discriminator, passcode and salt are values Matter accepts.

All findings are collected: a user should be able to fix a config in one
pass, not in five runs.
"""

from __future__ import annotations

import re
from fractions import Fraction

from mcuhome import ota, pairing, registry
from mcuhome.errors import ErrorCollector, Location
from mcuhome.schema import (
    RawBase,
    RawCluster,
    RawConfig,
    RawEndpoint,
    RawMatter,
    assign_endpoint_ids,
)
from mcuhome.toolchain import resolve_toolchain

__all__ = ["INT16S_MAX", "INT16S_MIN", "PAIRING_KEYS", "validate"]

#: Range of the ``int16s`` attributes every measurement cluster uses.
INT16S_MIN = -32768
INT16S_MAX = 32767

_ALIAS_RE = re.compile(r"^[a-z][a-z0-9_]*$")

#: Thread device roles the runtime implements today. SED/SSED land with
#: the power-management phase (components/matter/Kconfig offers Router
#: and Minimal End Device only).
SUPPORTED_THREAD_ROLES = ("ftd", "mtd")


def validate(config: RawConfig) -> None:
    """Run stage 2. Raises on the first *group* of problems found."""
    errors = ErrorCollector()

    _check_scope_gates(config, errors)
    _check_device(config, errors)
    _check_network(config, errors)
    _check_pairing(config, errors)
    _check_hardware(config, errors)
    _check_node(config, errors)

    errors.raise_if_any()


# --------------------------------------------------------------------------
# v0.1 scope gates
# --------------------------------------------------------------------------


def _check_scope_gates(config: RawConfig, errors: ErrorCollector) -> None:
    if config.automations:
        errors.add(
            '"automations:" is not implemented yet.',
            location=config.automations_loc or config.loc,
            hint=(
                "the automation engine is designed but not built yet — remove the "
                "automations: section for now"
            ),
        )

    network = config.network
    if network is not None:
        if network.coap is not None:
            errors.add(
                '"coap:" is not implemented yet.',
                location=network.coap,
                hint=(
                    "the CoAP maintenance channel is deferred (ADR 0010); Matter is the "
                    "integration path today — remove the coap: section"
                ),
            )
        if network.wifi is not None:
            errors.add(
                "WiFi is not supported yet.",
                location=network.wifi.loc,
                hint=(
                    "MCUHome builds Thread nodes today — configure a Thread transport "
                    "instead:\n    network:\n      thread:\n        device_role: ftd"
                ),
            )
        thread = network.thread
        if thread is not None and thread.device_role in ("sed", "ssed"):
            errors.add(
                f'Sleepy end devices ("device_role: {thread.device_role}") are not supported yet.',
                location=thread.loc_of("device_role"),
                hint=(
                    "sleepy operation lands with the power-management phase — for now use "
                    "device_role: ftd (mains-powered router) or device_role: mtd "
                    "(always-on end device)"
                ),
            )
        if (
            thread is not None
            and thread.poll_interval_ms is not None
            and thread.device_role in SUPPORTED_THREAD_ROLES
        ):
            errors.add(
                '"poll_interval:" only applies to sleepy end devices.',
                location=thread.loc_of("poll_interval"),
                hint=(
                    f'a "{thread.device_role}" node keeps its receiver on and never polls '
                    "— remove the poll_interval: line"
                ),
            )

    for endpoint in _endpoints(config):
        for cluster in endpoint.clusters.values():
            if cluster.target is not None:
                errors.add(
                    f'Actuators ("target:" on {cluster.name}) are not supported yet.',
                    location=cluster.loc_of("target"),
                    hint=(
                        "the channel layer can read sensors but not drive outputs yet "
                        "(the write path is an open point of the component model) — this "
                        "endpoint cannot be built today"
                    ),
                )
            if cluster.report is not None and cluster.report.max_interval_ms is not None:
                errors.add(
                    '"max_interval:" (heartbeat reporting) is not implemented yet.',
                    location=cluster.report.loc_of("max_interval"),
                    hint=(
                        "the runtime reports on change only — remove max_interval: and "
                        "use report: delta: to control how much traffic the node makes"
                    ),
                )


# --------------------------------------------------------------------------
# device: and toolchain
# --------------------------------------------------------------------------


def _check_device(config: RawConfig, errors: ErrorCollector) -> None:
    device = config.device
    board = device.board
    if board is not None and board not in registry.BOARDS:
        supported = ", ".join(sorted(registry.BOARDS))
        if board in registry.PLANNED_BOARDS:
            errors.add(
                f'MCUHome does not support the board "{board}" yet.',
                location=device.loc_of("board"),
                hint=(
                    f"{registry.PLANNED_BOARDS[board]}; boards MCUHome has brought up: {supported}"
                ),
            )
        else:
            errors.add(
                f'"{board}" is not a board MCUHome knows.',
                location=device.loc_of("board"),
                hint=(
                    f"boards MCUHome supports today: {supported} — the value is the Zephyr "
                    "board target string, verbatim"
                ),
            )

    if device.version is not None:
        problem = ota.describe_version_problem(device.version)
        if problem is not None:
            errors.add(
                f'"{device.version}" is not a usable device version.',
                location=device.loc_of("version"),
                hint=(
                    f"{problem}. The version becomes three things at once: MCUboot's "
                    "image version, the Matter SoftwareVersion a controller compares "
                    "when it decides whether an update is newer, and the version in "
                    "the .ota file — so it has to be a number MCUHome can map.\n"
                    f'    version: "{ota.DEFAULT_VERSION}"'
                ),
            )

    resolve_toolchain(
        board=board,
        blob_usage=device.blob_usage,
        zephyr_version=device.zephyr_version,
        blobs=device.blobs,
        blob_locs=device.blob_locs,
        version_loc=device.loc_of("zephyr_version"),
        errors=errors,
    )


# --------------------------------------------------------------------------
# network:
# --------------------------------------------------------------------------


def _check_network(config: RawConfig, errors: ErrorCollector) -> None:
    network = config.network
    if network is None:
        return

    transports = [name for name in ("thread", "wifi") if getattr(network, name) is not None]
    if len(transports) > 1:
        errors.add(
            "A device uses one transport, but this configuration has two.",
            location=network.wifi.loc if network.wifi else network.loc,
            hint="keep either thread: or wifi:, not both",
        )
    elif not transports:
        errors.add(
            '"network:" configures no transport.',
            location=network.loc,
            hint=(
                "add a transport:\n    network:\n      thread:\n        device_role: ftd\n"
                "  (or remove the network: section for a device that runs standalone)"
            ),
        )

    board = registry.BOARDS.get(config.device.board or "")
    if board is not None and network.thread is not None and "thread" not in board.transports:
        errors.add(
            f'The board "{board.name}" has no Thread radio.',
            location=network.thread.loc,
            hint="choose a board with an 802.15.4 radio, or remove the thread: section",
        )

    if _matter_enabled(config) is False and _endpoints(config):
        errors.add(
            "This configuration has endpoints, but Matter is switched off.",
            location=(network.matter.loc_of("enabled") if network.matter else network.loc),
            hint=(
                "without Matter nothing can read those endpoints yet (CoAP is deferred and "
                "automations are not implemented) — set matter: enabled: true, or remove "
                "the node: section"
            ),
        )


def _matter_enabled(config: RawConfig) -> bool:
    """Matter is on whenever a transport exists, unless switched off."""
    network = config.network
    if network is None:
        return False
    if network.matter is not None and network.matter.enabled is not None:
        return network.matter.enabled
    return network.thread is not None or network.wifi is not None


# --------------------------------------------------------------------------
# network.matter: commissioning credentials
# --------------------------------------------------------------------------

#: The three keys that make up one device's pairing identity. They are
#: written together and replaced together — a configuration carrying two
#: of them is a half-finished edit, not a shorthand.
PAIRING_KEYS = ("discriminator", "passcode", "salt")


def _check_pairing(config: RawConfig, errors: ErrorCollector) -> None:
    """The credential half of ``network.matter:``.

    MCUHome refuses to invent commissioning credentials at build time, and
    it refuses to fall back to the ones CHIP publishes. Both refusals have
    the same reason: a passcode everybody knows is a passcode that lets
    anybody commission the device, and a passcode the builder makes up
    behind the user's back is one they can never write down. So the
    configuration states them, and this function makes sure it does.
    """
    network = config.network
    matter = network.matter if network is not None else None
    enabled = _matter_enabled(config)
    device = config.device.name or "<device>"

    if matter is None:
        if enabled:
            _report_no_credentials(config, device, location_of=network, errors=errors)
        return

    given = [key for key in PAIRING_KEYS if getattr(matter, key) is not None]
    test = bool(matter.use_test_pairing)

    if not enabled:
        for key in [*given, *(["use_test_pairing"] if test else [])]:
            errors.add(
                f'"{key}:" only means something for a device that speaks Matter.',
                location=matter.loc_of(key),
                hint=(
                    f"commissioning is a Matter idea — switch Matter on, or remove the {key}: line"
                ),
            )
        return

    if test and given:
        errors.add(
            "This device asks for the test pairing code and brings its own at the same time.",
            location=matter.loc_of("use_test_pairing"),
            hint=(
                "keep one of the two: remove use_test_pairing: true to commission with "
                f"your own credentials, or remove {', '.join(f'{key}:' for key in given)} "
                "to use the published test ones"
            ),
        )
        return
    if test:
        return

    if not given:
        _report_no_credentials(config, device, location_of=matter, errors=errors)
        return

    missing = [key for key in PAIRING_KEYS if key not in given]
    if missing:
        errors.add(
            "The commissioning credentials are incomplete: "
            f"{', '.join(f'{key}:' for key in missing)} missing.",
            location=matter.loc_of(given[0]),
            hint=(
                "discriminator, passcode and salt belong together — write the missing "
                f"ones, or replace all three:\n    mcuhome init-pairing {device} --force"
            ),
        )
        return

    _check_discriminator(matter.discriminator, matter, errors)
    _check_passcode(matter.passcode, matter, device, errors)
    _check_salt(matter.salt, matter, device, errors)


def _report_no_credentials(
    config: RawConfig,
    device: str,
    *,
    location_of: RawBase | None,
    errors: ErrorCollector,
) -> None:
    errors.add(
        "This device has no commissioning credentials.",
        location=location_of.loc if location_of is not None else config.loc,
        hint=(
            "every Matter device needs a pairing code of its own — let the builder "
            f"draw one and write it into this file:\n    mcuhome init-pairing {device}\n"
            "  (on a bench device you can instead add use_test_pairing: true under "
            "matter:, which uses the passcode published with the Matter SDK — never "
            "on a device that leaves your desk)"
        ),
    )


def _check_discriminator(value: int | None, matter: RawMatter, errors: ErrorCollector) -> None:
    if value is None or 0 <= value <= pairing.DISCRIMINATOR_MAX:
        return
    errors.add(
        f'"discriminator: {value}" is outside the range Matter allows.',
        location=matter.loc_of("discriminator"),
        hint=(
            f"a discriminator is a number from 0 to {pairing.DISCRIMINATOR_MAX} "
            f"(0x000 to 0x{pairing.DISCRIMINATOR_MAX:03X}); it is how a commissioner "
            "picks this device out of everything else advertising itself"
        ),
    )


def _check_passcode(
    value: int | None, matter: RawMatter, device: str, errors: ErrorCollector
) -> None:
    if value is None:
        return
    if not pairing.PASSCODE_MIN <= value <= pairing.PASSCODE_MAX:
        errors.add(
            f'"passcode: {value}" is not a passcode Matter can carry.',
            location=matter.loc_of("passcode"),
            hint=(
                f"a passcode is a number from {pairing.PASSCODE_MIN} to "
                f"{pairing.PASSCODE_MAX} — or let the builder pick one:\n"
                f"    mcuhome init-pairing {device} --force"
            ),
        )
        return
    if value in pairing.FORBIDDEN_PASSCODES:
        errors.add(
            f'"passcode: {value:08d}" is one of the passcodes Matter forbids.',
            location=matter.loc_of("passcode"),
            hint=(
                "the specification rules out the twelve most guessable codes — eight "
                "repeated digits, 12345678 and 87654321 — because they are the first "
                f"thing anyone tries; pick another, or run:\n"
                f"    mcuhome init-pairing {device} --force"
            ),
        )


def _check_salt(value: str | None, matter: RawMatter, device: str, errors: ErrorCollector) -> None:
    if value is None:
        return
    problem = pairing.describe_salt_problem(value)
    if problem is None:
        return
    detail = (
        "it is not base64 text"
        if problem == "not base64"
        else f"this one is {problem} once decoded"
    )
    errors.add(
        f'"salt:" is not a usable commissioning salt: {detail}.',
        location=matter.loc_of("salt"),
        hint=(
            f"the salt is {pairing.SALT_MIN_BYTES} to {pairing.SALT_MAX_BYTES} random "
            "bytes written in base64, and it is what stops one precomputed table from "
            f"unlocking every device — the builder makes one for you:\n"
            f"    mcuhome init-pairing {device} --force"
        ),
    )


# --------------------------------------------------------------------------
# hardware:
# --------------------------------------------------------------------------


def _check_hardware(config: RawConfig, errors: ErrorCollector) -> None:
    hardware = config.hardware
    if hardware is None:
        return

    for bus in hardware.buses.values():
        if bus.sda is not None or bus.scl is not None:
            errors.add(
                f'Assigning bus pins ("sda:"/"scl:" on {bus.id}) is not implemented yet.',
                location=bus.loc_of("sda" if bus.sda is not None else "scl"),
                hint=(
                    "MCUHome can use a bus the board already routes, but cannot re-assign "
                    "pins yet — name the board's bus node instead:\n"
                    f"    {bus.id}:\n      controller: arduino_i2c"
                ),
            )
        elif bus.controller is None:
            errors.add(
                f'The bus "{bus.id}" does not say which bus of the board it is.',
                location=bus.loc,
                hint=(f"name the board's bus node:\n    {bus.id}:\n      controller: arduino_i2c"),
            )

    for peripheral in hardware.peripherals.values():
        driver_name = peripheral.driver
        if driver_name is None:
            continue
        driver = registry.DRIVERS.get(driver_name)
        if driver is None:
            _report_unknown_driver(peripheral.loc_of("driver"), driver_name, errors)
            continue

        if driver.bus is not None:
            if peripheral.bus is None:
                errors.add(
                    f'The peripheral "{peripheral.id}" does not say which bus it is on.',
                    location=peripheral.loc,
                    hint=(
                        f"add bus: <name of a bus from hardware.buses> — {driver_name} is "
                        f"an {driver.bus.upper()} device"
                    ),
                )
            elif peripheral.bus not in hardware.buses:
                known = ", ".join(sorted(hardware.buses)) or "none defined"
                errors.add(
                    f'The peripheral "{peripheral.id}" is on a bus called '
                    f'"{peripheral.bus}", which does not exist.',
                    location=peripheral.loc_of("bus"),
                    hint=f"buses defined in this configuration: {known}",
                )

        if driver.fixed_address is not None and peripheral.address not in (
            None,
            driver.fixed_address,
        ):
            errors.add(
                f'The address of "{peripheral.id}" cannot be changed.',
                location=peripheral.loc_of("address"),
                hint=(
                    f"a {driver_name} answers to {hex(driver.fixed_address)} in silicon — "
                    f"write address: {hex(driver.fixed_address)} or leave the line out"
                ),
            )

        for prop, value in peripheral.props.items():
            expected = driver.properties.get(prop)
            if expected is None:
                known = ", ".join(sorted(driver.properties)) or "none"
                errors.add(
                    f'"{driver_name}" has no setting called "{prop}".',
                    location=peripheral.loc_of(prop),
                    hint=f"settings this driver accepts: {known}",
                )
            elif not _value_matches(value, expected):
                errors.add(
                    f'"{prop}:" must be {_type_name(expected)}.',
                    location=peripheral.loc_of(prop),
                    hint=f"see the devicetree binding of {driver_name}",
                )


def _value_matches(value: object, expected: type) -> bool:
    # bool is a subclass of int in Python; devicetree is not that relaxed.
    if isinstance(value, bool) and expected is not bool:
        return False
    return isinstance(value, expected)


def _type_name(expected: type) -> str:
    return {int: "a whole number", str: "text", bool: "true or false"}.get(
        expected, expected.__name__
    )


def _report_unknown_driver(location: Location, driver_name: str, errors: ErrorCollector) -> None:
    supported = ", ".join(sorted(registry.DRIVERS))
    if driver_name in registry.PLANNED_DRIVERS:
        errors.add(
            f'MCUHome cannot build "{driver_name}" peripherals yet.',
            location=location,
            hint=f"{registry.PLANNED_DRIVERS[driver_name]}; supported today: {supported}",
        )
    else:
        errors.add(
            f'"{driver_name}" is not a driver MCUHome knows.',
            location=location,
            hint=(
                f"drivers supported today: {supported} — the value is the devicetree "
                "compatible string of the chip, verbatim"
            ),
        )


# --------------------------------------------------------------------------
# node:
# --------------------------------------------------------------------------


def _endpoints(config: RawConfig) -> list[RawEndpoint]:
    return config.node.endpoints if config.node is not None else []


def _check_node(config: RawConfig, errors: ErrorCollector) -> None:
    endpoints = _endpoints(config)
    if not endpoints:
        return

    ids = assign_endpoint_ids(config.node)
    seen_ids: dict[int, RawEndpoint] = {}
    seen_aliases: dict[str, RawEndpoint] = {}

    for endpoint in endpoints:
        endpoint_id = ids[endpoint.index]
        previous = seen_ids.get(endpoint_id)
        if previous is not None:
            errors.add(
                f"Endpoint {endpoint_id} is defined twice.",
                location=endpoint.loc_of("id"),
                hint="every endpoint needs its own id: — 1, 2, 3, …",
            )
        seen_ids[endpoint_id] = endpoint

        if endpoint.alias is not None:
            if not _ALIAS_RE.match(endpoint.alias):
                errors.add(
                    f'"{endpoint.alias}" is not a usable alias.',
                    location=endpoint.loc_of("alias"),
                    hint=(
                        "an alias starts with a lowercase letter and continues with "
                        "lowercase letters, digits or underscores — for example "
                        "alias: temperature"
                    ),
                )
            elif endpoint.alias in seen_aliases:
                errors.add(
                    f'The alias "{endpoint.alias}" is used twice.',
                    location=endpoint.loc_of("alias"),
                    hint="aliases name one endpoint each; pick a different name",
                )
            else:
                seen_aliases[endpoint.alias] = endpoint

        _check_endpoint(config, endpoint, endpoint_id, errors)


def _check_endpoint(
    config: RawConfig,
    endpoint: RawEndpoint,
    endpoint_id: int,
    errors: ErrorCollector,
) -> None:
    device_type = registry.DEVICE_TYPES.get(endpoint.device_type or "")
    if endpoint.device_type is not None and device_type is None:
        supported = ", ".join(sorted(registry.DEVICE_TYPES))
        if endpoint.device_type in registry.PLANNED_DEVICE_TYPES:
            errors.add(
                f'MCUHome cannot build "{endpoint.device_type}" endpoints yet.',
                location=endpoint.loc_of("device_type"),
                hint=(
                    f"{registry.PLANNED_DEVICE_TYPES[endpoint.device_type]}; device types "
                    f"supported today: {supported}"
                ),
            )
        else:
            errors.add(
                f'"{endpoint.device_type}" is not a device type MCUHome knows.',
                location=endpoint.loc_of("device_type"),
                hint=f"device types supported today: {supported}",
            )

    if not endpoint.clusters:
        errors.add(
            f"Endpoint {endpoint_id} has no clusters.",
            location=endpoint.loc,
            hint=(
                "an endpoint needs at least the cluster its device type requires, for "
                "example:\n    clusters:\n      temperature_measurement:\n"
                "        source: <peripheral>.temperature"
            ),
        )

    for cluster in endpoint.clusters.values():
        _check_cluster(config, cluster, endpoint_id, errors)

    if device_type is not None:
        for required in device_type.mandatory_clusters:
            if required not in endpoint.clusters:
                errors.add(
                    f'A "{device_type.name}" endpoint must have the "{required}" cluster.',
                    location=endpoint.loc,
                    hint=(
                        f"Matter requires it for this device type — add:\n"
                        f"    clusters:\n      {required}:\n"
                        f"        source: <peripheral>.<channel>"
                    ),
                )


def _check_cluster(
    config: RawConfig,
    cluster: RawCluster,
    endpoint_id: int,
    errors: ErrorCollector,
) -> None:
    definition = registry.CLUSTERS.get(cluster.name)
    if definition is None:
        supported = ", ".join(sorted(registry.CLUSTERS))
        if cluster.name in registry.PLANNED_CLUSTERS:
            errors.add(
                f'MCUHome cannot build the "{cluster.name}" cluster yet.',
                location=cluster.loc,
                hint=(
                    f"{registry.PLANNED_CLUSTERS[cluster.name]}; clusters supported today: "
                    f"{supported}"
                ),
            )
        else:
            errors.add(
                f'"{cluster.name}" is not a cluster MCUHome knows.',
                location=cluster.loc,
                hint=f"clusters supported today: {supported}",
            )
        return

    if cluster.target is not None:
        return  # already reported by the actuator gate

    if cluster.source is None:
        errors.add(
            f'The "{cluster.name}" cluster on endpoint {endpoint_id} has no source.',
            location=cluster.loc,
            hint=(
                "say which peripheral reading feeds it:\n"
                f"    {cluster.name}:\n      source: <peripheral>.{definition.quantity}"
            ),
        )
    else:
        _check_source(config, cluster, definition, errors)

    _check_range(cluster, definition, errors)
    _check_delta(cluster, definition, errors)


def _check_source(
    config: RawConfig,
    cluster: RawCluster,
    definition: registry.ClusterDef,
    errors: ErrorCollector,
) -> None:
    source = cluster.source or ""
    location = cluster.loc_of("source")
    if "." not in source:
        errors.add(
            f'"{source}" does not name a peripheral reading.',
            location=location,
            hint=(
                "a source is <peripheral>.<channel> — for example "
                f"source: my_sensor.{definition.quantity}"
            ),
        )
        return

    peripheral_id, channel_name = source.split(".", 1)
    peripherals = config.hardware.peripherals if config.hardware is not None else {}
    peripheral = peripherals.get(peripheral_id)
    if peripheral is None:
        known = ", ".join(sorted(peripherals)) or "none defined"
        errors.add(
            f'There is no peripheral called "{peripheral_id}".',
            location=location,
            hint=f"peripherals in this configuration: {known}",
        )
        return

    driver = registry.DRIVERS.get(peripheral.driver or "")
    if driver is None:
        return  # already reported by the driver check

    channel = driver.channels.get(channel_name)
    if channel is None:
        known = ", ".join(sorted(driver.channels))
        errors.add(
            f'"{peripheral_id}" has no reading called "{channel_name}".',
            location=location,
            hint=f"{peripheral.driver} provides: {known}",
        )
        return

    if channel.quantity != definition.quantity:
        errors.add(
            f'"{source}" measures {channel.quantity}, but the {cluster.name} cluster '
            f"reports {definition.quantity}.",
            location=location,
            hint=f"wire a {definition.quantity} reading to this cluster",
        )


def _to_raw(value: float, definition: registry.ClusterDef) -> int:
    return int(round(Fraction(value).limit_denominator(1_000_000) * definition.raw_per_unit))


def _check_range(
    cluster: RawCluster,
    definition: registry.ClusterDef,
    errors: ErrorCollector,
) -> None:
    if cluster.range is None:
        return
    for bound, value in (("min", cluster.range.min), ("max", cluster.range.max)):
        raw = _to_raw(value, definition)
        if not INT16S_MIN <= raw <= INT16S_MAX:
            limit_low = INT16S_MIN / float(definition.raw_per_unit)
            limit_high = INT16S_MAX / float(definition.raw_per_unit)
            errors.add(
                f"{value} {definition.unit} is outside what the {cluster.name} cluster can report.",
                location=cluster.range.loc_of(bound),
                hint=(f"the attribute holds {limit_low:g} to {limit_high:g} {definition.unit}"),
            )


def _check_delta(
    cluster: RawCluster,
    definition: registry.ClusterDef,
    errors: ErrorCollector,
) -> None:
    report = cluster.report
    if report is None or report.delta is None:
        return
    if report.delta < 0:
        errors.add(
            '"delta:" cannot be negative.',
            location=report.loc_of("delta"),
            hint="a delta is how far a reading must move before it is reported",
        )
        return
    raw = _to_raw(report.delta, definition)
    if report.delta > 0 and raw == 0:
        smallest = 1 / float(definition.raw_per_unit)
        errors.add(
            f"A delta of {report.delta} {definition.unit} is finer than the "
            f"{cluster.name} cluster can report.",
            location=report.loc_of("delta"),
            hint=(
                f"the smallest step this attribute carries is {smallest:g} "
                f"{definition.unit} — use that or more, or drop the delta: line to report "
                "every sample"
            ),
        )
