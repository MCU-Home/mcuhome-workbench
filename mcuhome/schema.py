# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Typed model of the *raw* configuration (yaml-schema.md).

This is stage 2's first half: shape. It turns the ruamel tree into
dataclasses that mirror the YAML one-to-one — no defaults are applied, no
Matter knowledge is used, nothing is derived. That happens in
:mod:`mcuhome.resolve`, which consumes what this module produces.

Every raw object keeps the source location of each of its keys
(:meth:`RawBase.loc_of`), so a later stage can point at the exact line
that caused a problem even though it no longer sees the YAML.

Shape checking is deliberately generous where the knowledge lives
elsewhere: an unknown peripheral property is *not* an error here, because
which properties exist depends on the driver, and drivers live in
:mod:`mcuhome.registry`. Shape errors are the ones a user can understand
without knowing anything about Matter or Zephyr: wrong type, unknown
section, malformed duration, value outside a fixed vocabulary.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcuhome.errors import ConfigError, ErrorCollector, Location

__all__ = [
    "RawBase",
    "RawBus",
    "RawCluster",
    "RawConfig",
    "RawDevice",
    "RawEndpoint",
    "RawHardware",
    "RawMatter",
    "RawNetwork",
    "RawNodeModel",
    "RawPeripheral",
    "RawPower",
    "RawRange",
    "RawReport",
    "RawThread",
    "RawWifi",
    "assign_endpoint_ids",
    "parse_config",
]


def assign_endpoint_ids(node: RawNodeModel | None) -> dict[int, int]:
    """Map each endpoint's list index to its Matter endpoint ID.

    ``id:`` is optional; endpoints without one are numbered from 1 in
    document order. Endpoint 0 is never assigned — it is the root node,
    statically compiled into the framework (ADR 0014).
    """
    if node is None:
        return {}
    return {
        endpoint.index: (endpoint.id if endpoint.id is not None else endpoint.index + 1)
        for endpoint in node.endpoints
    }


DEVICE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
DEVICE_NAME_MAX = 32

_DURATION_UNITS = {
    "ms": 1,
    "s": 1_000,
    "min": 60_000,
    "h": 3_600_000,
    "d": 86_400_000,
}
_DURATION_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*(ms|s|min|h|d)$")

_FREQUENCY_UNITS = {"hz": 1, "khz": 1_000, "mhz": 1_000_000}
_FREQUENCY_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*(hz|khz|mhz)$", re.IGNORECASE)

_PIN_RE = re.compile(r"^gpio\d+\.\d+$")

#: Top-level keys reserved for a later schema revision (yaml-schema.md §9).
PLANNED_SECTIONS = {
    "packages": "packages and includes arrive with schema revision 2",
    "substitutions": "substitutions arrive with schema revision 2",
}


# --------------------------------------------------------------------------
# Raw dataclasses
# --------------------------------------------------------------------------


@dataclass(kw_only=True)
class RawBase:
    """Common base: where this object came from."""

    loc: Location
    locs: dict[str, Location] = field(default_factory=dict)

    def loc_of(self, key: str) -> Location:
        """Location of *key*'s value, falling back to the object itself."""
        return self.locs.get(key, self.loc)


@dataclass(kw_only=True)
class RawPower(RawBase):
    source: str | None = None


@dataclass(kw_only=True)
class RawDevice(RawBase):
    name: str | None = None
    friendly_name: str | None = None
    board: str | None = None
    #: SemVer string, as written. Range-checked in
    #: :mod:`mcuhome.validate`, where the message can explain what the
    #: number becomes.
    version: str | None = None
    power: RawPower | None = None
    blob_usage: str | None = None
    zephyr_version: str | None = None
    blobs: dict[str, str] = field(default_factory=dict)
    blob_locs: dict[str, Location] = field(default_factory=dict)


@dataclass(kw_only=True)
class RawThread(RawBase):
    device_role: str | None = None
    poll_interval_ms: int | None = None


@dataclass(kw_only=True)
class RawWifi(RawBase):
    ssid: str | None = None
    password: str | None = None


@dataclass(kw_only=True)
class RawMatter(RawBase):
    enabled: bool | None = None
    #: Commissioning credentials, as written. Shape only here — the
    #: ranges Matter puts on them are checked in :mod:`mcuhome.validate`,
    #: where the message can explain what the number is for.
    discriminator: int | None = None
    passcode: int | None = None
    salt: str | None = None
    use_test_pairing: bool | None = None


@dataclass(kw_only=True)
class RawNetwork(RawBase):
    thread: RawThread | None = None
    wifi: RawWifi | None = None
    matter: RawMatter | None = None
    coap: Location | None = None


@dataclass(kw_only=True)
class RawBus(RawBase):
    id: str
    controller: str | None = None
    sda: str | None = None
    scl: str | None = None
    frequency_hz: int | None = None


@dataclass(kw_only=True)
class RawPeripheral(RawBase):
    id: str
    driver: str | None = None
    bus: str | None = None
    address: int | None = None
    #: Everything else under the peripheral. Validated against the
    #: driver's property table once the driver is known.
    props: dict[str, Any] = field(default_factory=dict)


@dataclass(kw_only=True)
class RawHardware(RawBase):
    buses: dict[str, RawBus] = field(default_factory=dict)
    peripherals: dict[str, RawPeripheral] = field(default_factory=dict)


@dataclass(kw_only=True)
class RawRange(RawBase):
    min: float
    max: float


@dataclass(kw_only=True)
class RawReport(RawBase):
    delta: float | None = None
    max_interval_ms: int | None = None


@dataclass(kw_only=True)
class RawCluster(RawBase):
    name: str
    source: str | None = None
    target: str | None = None
    sampling_ms: int | None = None
    report: RawReport | None = None
    range: RawRange | None = None


@dataclass(kw_only=True)
class RawEndpoint(RawBase):
    index: int
    id: int | None = None
    alias: str | None = None
    device_type: str | None = None
    clusters: dict[str, RawCluster] = field(default_factory=dict)


@dataclass(kw_only=True)
class RawNodeModel(RawBase):
    endpoints: list[RawEndpoint] = field(default_factory=list)


@dataclass(kw_only=True)
class RawAutomation(RawBase):
    id: str | None = None


@dataclass(kw_only=True)
class RawConfig(RawBase):
    file: Path
    device: RawDevice
    network: RawNetwork | None = None
    hardware: RawHardware | None = None
    node: RawNodeModel | None = None
    automations: list[RawAutomation] = field(default_factory=list)
    automations_loc: Location | None = None


# --------------------------------------------------------------------------
# Location-aware reader
# --------------------------------------------------------------------------


def _seq_item_loc(seq: Any, index: int, file: Path, path: str) -> Location:
    lc = getattr(seq, "lc", None)
    if lc is None:
        return Location(file=file, key=path)
    try:
        line, column = lc.item(index)
    except (KeyError, IndexError, TypeError):  # pragma: no cover - defensive
        return Location(file=file, key=path)
    return Location(file=file, line=line + 1, column=column + 1, key=path)


class MapReader:
    """Reads one YAML mapping, remembering where each value came from."""

    def __init__(
        self,
        data: dict[str, Any],
        *,
        file: Path,
        path: str,
        loc: Location,
        errors: ErrorCollector,
    ) -> None:
        self.data = data
        self.file = file
        self.path = path
        self.loc = loc
        self.errors = errors

    # -- locations --------------------------------------------------

    def child_path(self, key: str) -> str:
        return f"{self.path}.{key}" if self.path else str(key)

    def loc_of(self, key: str) -> Location:
        path = self.child_path(key)
        lc = getattr(self.data, "lc", None)
        if lc is not None and key in self.data:
            try:
                line, column = lc.value(key)
            except (KeyError, TypeError):  # pragma: no cover - defensive
                pass
            else:
                return Location(file=self.file, line=line + 1, column=column + 1, key=path)
        return self.loc.with_key(path)

    def key_loc(self, key: str) -> Location:
        path = self.child_path(key)
        lc = getattr(self.data, "lc", None)
        if lc is not None and key in self.data:
            try:
                line, column = lc.key(key)
            except (KeyError, TypeError):  # pragma: no cover - defensive
                pass
            else:
                return Location(file=self.file, line=line + 1, column=column + 1, key=path)
        return self.loc.with_key(path)

    def all_locs(self) -> dict[str, Location]:
        return {str(key): self.loc_of(str(key)) for key in self.data}

    # -- structure --------------------------------------------------

    def reject_unknown(self, allowed: set[str], *, planned: dict[str, str] | None = None) -> None:
        planned = planned or {}
        for key in self.data:
            name = str(key)
            if name in allowed:
                continue
            if name in planned:
                self.errors.add(
                    f'"{name}:" is not implemented yet.',
                    location=self.key_loc(name),
                    hint=f"{planned[name]}; remove the key for now",
                )
                continue
            self.errors.add(
                f'Unknown key "{name}".',
                location=self.key_loc(name),
                hint=f"keys allowed here: {', '.join(sorted(allowed))}",
            )

    def has(self, key: str) -> bool:
        return key in self.data

    def raw(self, key: str) -> Any:
        return self.data.get(key)

    def require_present(self, key: str, *, hint: str) -> bool:
        if key in self.data and self.data[key] is not None:
            return True
        self.errors.add(
            f'"{key}:" is required here but missing.',
            location=self.loc.with_key(self.child_path(key)),
            hint=hint,
        )
        return False

    # -- scalars ----------------------------------------------------

    def _wrong_type(self, key: str, expected: str, value: Any) -> None:
        self.errors.add(
            f'"{key}:" must be {expected}, but this is {_kind_of(value)}.',
            location=self.loc_of(key),
            hint=f"write {key}: <{expected}>",
        )

    def string(
        self,
        key: str,
        *,
        choices: tuple[str, ...] | None = None,
        required: bool = False,
        hint: str | None = None,
    ) -> str | None:
        if key not in self.data or self.data[key] is None:
            if required:
                self.require_present(key, hint=hint or f"add {key}: <value>")
            return None
        value = self.data[key]
        if isinstance(value, bool) or not isinstance(value, str):
            self._wrong_type(key, "text", value)
            return None
        if choices is not None and value not in choices:
            self.errors.add(
                f'"{value}" is not a valid value for {key}:.',
                location=self.loc_of(key),
                hint=f"use one of: {', '.join(choices)}",
            )
            return None
        return value

    def integer(self, key: str, *, minimum: int | None = None) -> int | None:
        if key not in self.data or self.data[key] is None:
            return None
        value = self.data[key]
        if isinstance(value, bool) or not isinstance(value, int):
            self._wrong_type(key, "a whole number", value)
            return None
        if minimum is not None and value < minimum:
            self.errors.add(
                f'"{key}:" must be {minimum} or greater.',
                location=self.loc_of(key),
                hint=f"use {key}: {minimum} or higher",
            )
            return None
        return int(value)

    def number(self, key: str) -> float | None:
        if key not in self.data or self.data[key] is None:
            return None
        value = self.data[key]
        if isinstance(value, bool) or not isinstance(value, int | float):
            self._wrong_type(key, "a number", value)
            return None
        return float(value)

    def boolean(self, key: str) -> bool | None:
        if key not in self.data or self.data[key] is None:
            return None
        value = self.data[key]
        if not isinstance(value, bool):
            self._wrong_type(key, "true or false", value)
            return None
        return bool(value)

    def duration_ms(self, key: str) -> int | None:
        if key not in self.data or self.data[key] is None:
            return None
        value = self.data[key]
        if isinstance(value, str):
            match = _DURATION_RE.match(value.strip())
            if match:
                amount = float(match.group(1)) * _DURATION_UNITS[match.group(2)]
                if amount <= 0:
                    self.errors.add(
                        f'"{key}: {value}" must be longer than zero.',
                        location=self.loc_of(key),
                        hint=f"for example {key}: 60s",
                    )
                    return None
                return int(round(amount))
        self.errors.add(
            f'"{key}:" must be a duration with a unit.',
            location=self.loc_of(key),
            hint=f"units are ms, s, min, h, d — for example {key}: 60s",
        )
        return None

    def frequency_hz(self, key: str) -> int | None:
        if key not in self.data or self.data[key] is None:
            return None
        value = self.data[key]
        if isinstance(value, str):
            match = _FREQUENCY_RE.match(value.strip())
            if match:
                return int(round(float(match.group(1)) * _FREQUENCY_UNITS[match.group(2).lower()]))
        self.errors.add(
            f'"{key}:" must be a bus frequency with a unit.',
            location=self.loc_of(key),
            hint=f"units are hz, khz, mhz — for example {key}: 400khz",
        )
        return None

    def pin(self, key: str) -> str | None:
        value = self.string(key)
        if value is None:
            return None
        if not _PIN_RE.match(value):
            self.errors.add(
                f'"{key}: {value}" is not a pin.',
                location=self.loc_of(key),
                hint=f"pins use Zephyr's port.pin notation — for example {key}: gpio1.11",
            )
            return None
        return value

    # -- containers -------------------------------------------------

    def mapping(self, key: str) -> MapReader | None:
        if key not in self.data or self.data[key] is None:
            return None
        value = self.data[key]
        if not isinstance(value, dict):
            self._wrong_type(key, "a group of keys", value)
            return None
        return MapReader(
            value,
            file=self.file,
            path=self.child_path(key),
            # The *key* line, not the first line of the body: an error
            # about a whole section reads better pointing at the line
            # that names it.
            loc=self.key_loc(key),
            errors=self.errors,
        )

    def sequence(self, key: str) -> list[tuple[Any, Location]] | None:
        if key not in self.data or self.data[key] is None:
            return None
        value = self.data[key]
        if not isinstance(value, list):
            self._wrong_type(key, "a list", value)
            return None
        path = self.child_path(key)
        return [
            (item, _seq_item_loc(value, index, self.file, f"{path}[{index}]"))
            for index, item in enumerate(value)
        ]

    def sub_reader(self, value: dict[str, Any], path: str, loc: Location) -> MapReader:
        return MapReader(value, file=self.file, path=path, loc=loc, errors=self.errors)


def _kind_of(value: Any) -> str:
    if value is None:
        return "empty"
    if isinstance(value, bool):
        return "true/false"
    if isinstance(value, dict):
        return "a group of keys"
    if isinstance(value, list):
        return "a list"
    if isinstance(value, str):
        return "text"
    if isinstance(value, int | float):
        return "a number"
    return type(value).__name__  # pragma: no cover - defensive


# --------------------------------------------------------------------------
# Section parsers
# --------------------------------------------------------------------------


def _parse_power(reader: MapReader) -> RawPower:
    reader.reject_unknown({"source"})
    return RawPower(
        loc=reader.loc,
        locs=reader.all_locs(),
        source=reader.string("source", choices=("battery", "mains")),
    )


def _parse_device(reader: MapReader) -> RawDevice:
    reader.reject_unknown(
        {
            "name",
            "friendly_name",
            "board",
            "version",
            "power",
            "blob_usage",
            "zephyr_version",
            "blobs",
        }
    )

    name = reader.string(
        "name",
        required=True,
        hint="add name: my-sensor — lowercase letters, digits and dashes; it becomes the hostname",
    )
    if name is not None and (
        not DEVICE_NAME_RE.match(name) or name.endswith("-") or len(name) > DEVICE_NAME_MAX
    ):
        reader.errors.add(
            f'"{name}" is not a usable device name.',
            location=reader.loc_of("name"),
            hint=(
                f"use lowercase letters, digits and dashes, at most {DEVICE_NAME_MAX} "
                "characters, not starting or ending with a dash — the name becomes the "
                "node's hostname"
            ),
        )
        name = None

    power_reader = reader.mapping("power")
    zephyr_version = _parse_zephyr_version(reader)
    version = _parse_version(reader)

    blobs: dict[str, str] = {}
    blob_locs: dict[str, Location] = {}
    blobs_reader = reader.mapping("blobs")
    if blobs_reader is not None:
        for key in list(blobs_reader.data):
            blob = str(key)
            value = blobs_reader.string(blob, choices=("enabled", "disabled", "auto"))
            blob_locs[blob] = blobs_reader.loc_of(blob)
            if value is not None:
                blobs[blob] = value

    return RawDevice(
        loc=reader.loc,
        locs=reader.all_locs(),
        name=name,
        friendly_name=reader.string("friendly_name"),
        board=reader.string(
            "board",
            required=True,
            hint="add board: nrf7002dk/nrf5340/cpuapp — the Zephyr board target, verbatim",
        ),
        version=version,
        power=_parse_power(power_reader) if power_reader is not None else None,
        blob_usage=reader.string("blob_usage", choices=("auto", "none")),
        zephyr_version=zephyr_version,
        blobs=blobs,
        blob_locs=blob_locs,
    )


def _parse_version(reader: MapReader) -> str | None:
    """``device.version``, as a string, or None when it is not written.

    Shape only: the SemVer rules and the range Matter's SoftwareVersion
    puts on the three fields are checked in :mod:`mcuhome.validate`, where
    the message can say what the number is for. What happens here is the
    YAML-scalar problem — an unquoted ``1.4.0`` is a string but an
    unquoted ``1.4`` is a float, and telling a user to learn YAML's scalar
    rules is not an error message.
    """
    if not reader.has("version"):
        return None
    raw = reader.raw("version")
    if isinstance(raw, float | int) and not isinstance(raw, bool):
        reader.errors.add(
            f"{raw} is not a device version.",
            location=reader.loc_of("version"),
            hint=(
                "a version is three numbers separated by dots, and YAML reads that "
                'as text only in quotes:\n    version: "1.4.0"'
            ),
        )
        return None
    return reader.string("version")


_ZEPHYR_LINE_RE = re.compile(r"^\d+\.\d+$")


def _parse_zephyr_version(reader: MapReader) -> str | None:
    if not reader.has("zephyr_version"):
        return None
    raw = reader.raw("zephyr_version")
    # 4.4 unquoted parses as a float; accept it and say so, rather than
    # making the user learn YAML's scalar rules.
    if isinstance(raw, float | int) and not isinstance(raw, bool):
        return f"{raw}"
    value = reader.string("zephyr_version")
    if value is None:
        return None
    if value in ("auto", "latest") or _ZEPHYR_LINE_RE.match(value):
        return value
    reader.errors.add(
        f'"{value}" is not a Zephyr version.',
        location=reader.loc_of("zephyr_version"),
        hint='use auto (recommended), latest, or a release line such as "4.4"',
    )
    return None


def _parse_network(reader: MapReader) -> RawNetwork:
    reader.reject_unknown({"thread", "wifi", "matter", "coap"})

    thread: RawThread | None = None
    thread_reader = reader.mapping("thread")
    if thread_reader is not None:
        thread_reader.reject_unknown({"device_role", "poll_interval"})
        thread = RawThread(
            loc=thread_reader.loc,
            locs=thread_reader.all_locs(),
            device_role=thread_reader.string("device_role", choices=("ftd", "mtd", "sed", "ssed")),
            poll_interval_ms=thread_reader.duration_ms("poll_interval"),
        )

    wifi: RawWifi | None = None
    wifi_reader = reader.mapping("wifi")
    if wifi_reader is not None:
        wifi_reader.reject_unknown({"ssid", "password"})
        wifi = RawWifi(
            loc=wifi_reader.loc,
            locs=wifi_reader.all_locs(),
            ssid=wifi_reader.string("ssid"),
            password=wifi_reader.string("password"),
        )

    matter: RawMatter | None = None
    matter_reader = reader.mapping("matter")
    if matter_reader is not None:
        matter_reader.reject_unknown(
            {"enabled", "discriminator", "passcode", "salt", "use_test_pairing"}
        )
        matter = RawMatter(
            loc=matter_reader.loc,
            locs=matter_reader.all_locs(),
            enabled=matter_reader.boolean("enabled"),
            discriminator=matter_reader.integer("discriminator"),
            passcode=matter_reader.integer("passcode"),
            salt=matter_reader.string("salt"),
            use_test_pairing=matter_reader.boolean("use_test_pairing"),
        )

    return RawNetwork(
        loc=reader.loc,
        locs=reader.all_locs(),
        thread=thread,
        wifi=wifi,
        matter=matter,
        coap=reader.key_loc("coap") if reader.has("coap") else None,
    )


def _parse_hardware(reader: MapReader) -> RawHardware:
    reader.reject_unknown({"buses", "peripherals"})

    buses: dict[str, RawBus] = {}
    buses_reader = reader.mapping("buses")
    if buses_reader is not None:
        for key in list(buses_reader.data):
            bus_id = str(key)
            bus_reader = buses_reader.mapping(bus_id)
            if bus_reader is None:
                continue
            bus_reader.reject_unknown({"controller", "sda", "scl", "frequency"})
            buses[bus_id] = RawBus(
                loc=bus_reader.loc,
                locs=bus_reader.all_locs(),
                id=bus_id,
                controller=bus_reader.string("controller"),
                sda=bus_reader.pin("sda"),
                scl=bus_reader.pin("scl"),
                frequency_hz=bus_reader.frequency_hz("frequency"),
            )

    peripherals: dict[str, RawPeripheral] = {}
    peripherals_reader = reader.mapping("peripherals")
    if peripherals_reader is not None:
        for key in list(peripherals_reader.data):
            peripheral_id = str(key)
            peripheral_reader = peripherals_reader.mapping(peripheral_id)
            if peripheral_reader is None:
                continue
            props = {
                str(prop): peripheral_reader.raw(str(prop))
                for prop in peripheral_reader.data
                if str(prop) not in ("driver", "bus", "address")
            }
            peripherals[peripheral_id] = RawPeripheral(
                loc=peripheral_reader.loc,
                locs=peripheral_reader.all_locs(),
                id=peripheral_id,
                driver=peripheral_reader.string(
                    "driver",
                    required=True,
                    hint=("add driver: <devicetree compatible> — for example driver: bosch,bmp180"),
                ),
                bus=peripheral_reader.string("bus"),
                address=peripheral_reader.integer("address", minimum=0),
                props=props,
            )

    return RawHardware(
        loc=reader.loc,
        locs=reader.all_locs(),
        buses=buses,
        peripherals=peripherals,
    )


def _parse_cluster(reader: MapReader, name: str) -> RawCluster:
    reader.reject_unknown({"source", "target", "sampling", "report", "range"})

    report: RawReport | None = None
    report_reader = reader.mapping("report")
    if report_reader is not None:
        report_reader.reject_unknown({"delta", "max_interval"})
        report = RawReport(
            loc=report_reader.loc,
            locs=report_reader.all_locs(),
            delta=report_reader.number("delta"),
            max_interval_ms=report_reader.duration_ms("max_interval"),
        )

    value_range: RawRange | None = None
    range_reader = reader.mapping("range")
    if range_reader is not None:
        range_reader.reject_unknown({"min", "max"})
        range_reader.require_present("min", hint="add min: <lowest value the sensor reports>")
        range_reader.require_present("max", hint="add max: <highest value the sensor reports>")
        minimum = range_reader.number("min")
        maximum = range_reader.number("max")
        if minimum is not None and maximum is not None:
            if minimum >= maximum:
                range_reader.errors.add(
                    "The measurement range is empty: min is not below max.",
                    location=range_reader.loc_of("min"),
                    hint="min must be lower than max",
                )
            else:
                value_range = RawRange(
                    loc=range_reader.loc,
                    locs=range_reader.all_locs(),
                    min=minimum,
                    max=maximum,
                )

    return RawCluster(
        loc=reader.loc,
        locs=reader.all_locs(),
        name=name,
        source=reader.string("source"),
        target=reader.string("target"),
        sampling_ms=reader.duration_ms("sampling"),
        report=report,
        range=value_range,
    )


def _parse_node(reader: MapReader) -> RawNodeModel:
    reader.reject_unknown({"endpoints"}, planned={"bindings": "device-to-device bindings"})

    endpoints: list[RawEndpoint] = []
    items = reader.sequence("endpoints")
    if items is None:
        if reader.has("endpoints"):
            return RawNodeModel(loc=reader.loc, locs=reader.all_locs())
        reader.errors.add(
            '"node:" has no endpoints.',
            location=reader.loc,
            hint="add an endpoints: list, or remove the node: section entirely",
        )
        return RawNodeModel(loc=reader.loc, locs=reader.all_locs())

    for index, (item, item_loc) in enumerate(items):
        if not isinstance(item, dict):
            reader.errors.add(
                f"Endpoint {index + 1} must be a group of keys.",
                location=item_loc,
                hint="each endpoint starts with a dash, then its keys: - id: 1",
            )
            continue
        endpoint_reader = reader.sub_reader(item, item_loc.key or "", item_loc)
        endpoint_reader.reject_unknown({"id", "alias", "device_type", "clusters"})

        clusters: dict[str, RawCluster] = {}
        clusters_reader = endpoint_reader.mapping("clusters")
        if clusters_reader is not None:
            for key in list(clusters_reader.data):
                cluster_name = str(key)
                cluster_reader = clusters_reader.mapping(cluster_name)
                if cluster_reader is None:
                    continue
                clusters[cluster_name] = _parse_cluster(cluster_reader, cluster_name)

        endpoints.append(
            RawEndpoint(
                loc=item_loc,
                locs=endpoint_reader.all_locs(),
                index=index,
                id=endpoint_reader.integer("id", minimum=1),
                alias=endpoint_reader.string("alias"),
                device_type=endpoint_reader.string(
                    "device_type",
                    required=True,
                    hint="add device_type: temperature_sensor — a Matter device type name",
                ),
                clusters=clusters,
            )
        )

    return RawNodeModel(loc=reader.loc, locs=reader.all_locs(), endpoints=endpoints)


def parse_config(data: dict[str, Any], *, file: Path) -> RawConfig:
    """Parse a loaded YAML document into the raw configuration model.

    Raises :class:`~mcuhome.errors.ConfigError` /
    :class:`~mcuhome.errors.ConfigErrorGroup` if the shape is wrong.
    """
    errors = ErrorCollector()
    root = MapReader(
        data,
        file=file,
        path="",
        loc=Location(file=file, line=1, column=1),
        errors=errors,
    )
    root.reject_unknown(
        {"device", "network", "hardware", "node", "automations"},
        planned=PLANNED_SECTIONS,
    )

    device_reader = root.mapping("device")
    if device_reader is None:
        raise ConfigError(
            'This configuration has no "device:" section.',
            location=Location(file=file, line=1, column=1),
            hint=(
                "every device configuration starts with:\n"
                "    device:\n"
                "      name: my-sensor\n"
                "      board: nrf7002dk/nrf5340/cpuapp"
            ),
        )
    device = _parse_device(device_reader)

    network_reader = root.mapping("network")
    hardware_reader = root.mapping("hardware")
    node_reader = root.mapping("node")

    automations: list[RawAutomation] = []
    automation_items = root.sequence("automations")
    if automation_items:
        for item, item_loc in automation_items:
            automation_id = None
            if isinstance(item, dict):
                automation_id = item.get("id")
            automations.append(
                RawAutomation(
                    loc=item_loc,
                    id=str(automation_id) if automation_id is not None else None,
                )
            )

    config = RawConfig(
        loc=Location(file=file, line=1, column=1),
        locs=root.all_locs(),
        file=file,
        device=device,
        network=_parse_network(network_reader) if network_reader is not None else None,
        hardware=_parse_hardware(hardware_reader) if hardware_reader is not None else None,
        node=_parse_node(node_reader) if node_reader is not None else None,
        automations=automations,
        automations_loc=root.key_loc("automations") if root.has("automations") else None,
    )
    errors.raise_if_any()
    return config
