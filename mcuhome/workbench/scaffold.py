# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""``mcuhome new`` — the first file of a new device.

A device is a folder with a ``main.yaml`` in it (builder-pipeline.md §2),
and writing that first file from memory means knowing the board target
verbatim, which sections exist and which of them are optional. This module
writes it instead: the sections a device always has, filled in from the
board's own registry entry, and the ones it might have as commented,
copy-pasteable examples.

**What is active and what is commented, and why.** The generated file is
*complete* as it stands — a Matter node on the board's transport, with no
hardware. That validates and builds, which is what makes it a starting
point rather than a template with holes. Hardware and endpoints are
commented out because scaffolding a peripheral the user does not own is
worse than scaffolding none: they would have to delete it before their
first build, and deleting is a worse first task than uncommenting.

**Unless the caller already knows.** That reasoning is about *guessing*,
and it stops applying the moment somebody has said which sensor is wired
up: a caller may pass a :class:`DeviceOutline`, and then the hardware
and endpoints are written as real sections instead of as an example. A
form that walked a user through the registry's boards, drivers and
device types has answers the command line does not, and asking them to
retype the result into the commented block would be asking them to do the
work twice. Nothing else changes — the same file, the same order, the
same comments around it.

**What this checks, and what it leaves to the validator.** The outline is
checked for the things that would make this function write nonsense: a
name no registry table has, a reference to a bus or a peripheral the
outline does not contain, the same id twice. Everything *semantic* — a
cluster fed from a channel measuring the wrong quantity, a device type
missing a mandatory cluster, an address out of range — belongs to stages
1-3, which say it with a line and column in the file that now exists.
Two authorities on what a valid configuration is would be one too many.

**It does not draw commissioning credentials.** ``mcuhome device matter-pairing --new``
does that, in its own command, because those are per-device secrets that
are drawn once and then never again (yaml-schema.md §4.1) — a scaffold
that produced them as a side effect would make ``new`` an operation with
a consequence, and re-running it after a mistake would silently break
every controller that already knows the device. What this does instead is
say, in the file and in the command's output, that it is the next step.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from mcuhome.model import ota, registry
from mcuhome.model.errors import ConfigError, Location

from mcuhome.workbench import schema
from mcuhome.workbench.project import Project, resolve_project

__all__ = [
    "BusChoice",
    "ClusterChoice",
    "DeviceOutline",
    "EndpointChoice",
    "NewDevice",
    "PeripheralChoice",
    "new_device",
    "render_starter",
]


@dataclass(frozen=True)
class NewDevice:
    """What ``mcuhome new`` created."""

    project: Project
    entry: Path
    name: str
    board: str


@dataclass(frozen=True)
class BusChoice:
    """One entry under ``hardware.buses``."""

    #: The YAML key, and what a peripheral's ``bus:`` names.
    id: str
    #: The board's devicetree node label — ``registry.BoardBusDef.controller``.
    controller: str


@dataclass(frozen=True)
class PeripheralChoice:
    """One entry under ``hardware.peripherals``."""

    #: The YAML key, and the first half of a cluster's ``source``.
    id: str
    #: A key of :data:`mcuhome.model.registry.DRIVERS` — the devicetree
    #: compatible string, which is what ``driver:`` holds.
    driver: str
    #: Which :class:`BusChoice` it sits on, by id. ``None`` for a
    #: bus-less peripheral.
    bus: str | None = None
    #: Written only when given. A part with a fixed address does not
    #: need one, and the resolver fills in the rest.
    address: int | None = None


@dataclass(frozen=True)
class ClusterChoice:
    """One entry under an endpoint's ``clusters``."""

    #: A key of :data:`mcuhome.model.registry.CLUSTERS`.
    cluster: str
    #: ``<peripheral>.<channel>``, both of which must exist.
    source: str
    #: A duration as the schema spells it (``30s``), or ``None`` for the
    #: resolver's default. Written only when given: an explicit copy of
    #: a default is a value nobody chose that later stops matching.
    sampling: str | None = None


@dataclass(frozen=True)
class EndpointChoice:
    """One entry under ``node.endpoints``."""

    #: A key of :data:`mcuhome.model.registry.DEVICE_TYPES`.
    device_type: str
    clusters: tuple[ClusterChoice, ...] = ()


@dataclass(frozen=True)
class DeviceOutline:
    """The hardware and endpoints a caller already knows about.

    Empty — the default — means "write the commented example", which is
    what the command line does. A caller that walked somebody through
    the registry passes what they picked and gets it written out.
    """

    buses: tuple[BusChoice, ...] = ()
    peripherals: tuple[PeripheralChoice, ...] = ()
    endpoints: tuple[EndpointChoice, ...] = ()

    def is_empty(self) -> bool:
        return not (self.buses or self.peripherals or self.endpoints)


def _refuse_unknown_board(board: str) -> ConfigError:
    known = ", ".join(sorted(registry.BOARDS))
    planned = registry.PLANNED_BOARDS.get(board)
    if planned is not None:
        return ConfigError(
            f'MCUHome does not support the board "{board}" yet: {planned}.',
            hint=f"boards MCUHome supports today: {known} — mcuhome device boards lists them",
        )
    return ConfigError(
        f'"{board}" is not a board MCUHome knows.',
        hint=(
            f"use one of the boards MCUHome supports today: {known}\n"
            "The board name is the Zephyr board target, verbatim, qualifiers "
            "included; mcuhome device boards lists the supported and planned ones."
        ),
    )


def _refuse_bad_name(name: str) -> ConfigError:
    return ConfigError(
        f'"{name}" is not a usable device name.',
        hint=(
            f"use lowercase letters, digits and dashes, at most "
            f"{schema.DEVICE_NAME_MAX} characters, at least one letter, not starting "
            "or ending with a dash — the name becomes the device's folder and the "
            "node's hostname"
        ),
    )


def _refuse_unknown(kind: str, name: str, known: Mapping[str, object], planned: Mapping[str, str]):
    """One refusal shape for every registry table the outline names.

    The tables differ in what they hold and not in how being absent from
    one feels, so a planned entry says *why* it is not there yet and an
    unknown one lists what is.
    """
    reason = planned.get(name)
    if reason is not None:
        return ConfigError(
            f'MCUHome does not support the {kind} "{name}" yet: {reason}.',
            hint=f"{kind}s MCUHome supports today: {', '.join(sorted(known))}",
        )
    return ConfigError(
        f'"{name}" is not a {kind} MCUHome knows.',
        hint=f"use one of: {', '.join(sorted(known))}",
    )


def _listed(names) -> str:
    """``": a, b"``, or a phrase saying the outline listed none."""
    return f": {', '.join(sorted(names))}" if names else " — it lists none"


def _check_outline(outline: DeviceOutline) -> None:
    """Refuse an outline this module could only render as nonsense.

    Structure only — see the module docstring for the line between this
    and the validator.
    """
    bus_ids: set[str] = set()
    for bus in outline.buses:
        if bus.id in bus_ids:
            raise ConfigError(f'The outline names the bus "{bus.id}" twice.')
        bus_ids.add(bus.id)

    channels: dict[str, frozenset[str]] = {}
    for peripheral in outline.peripherals:
        if peripheral.id in channels:
            raise ConfigError(f'The outline names the peripheral "{peripheral.id}" twice.')
        driver = registry.DRIVERS.get(peripheral.driver)
        if driver is None:
            raise _refuse_unknown(
                "driver", peripheral.driver, registry.DRIVERS, registry.PLANNED_DRIVERS
            )
        if peripheral.bus is not None and peripheral.bus not in bus_ids:
            raise ConfigError(
                f'The peripheral "{peripheral.id}" sits on a bus called '
                f'"{peripheral.bus}", which the outline does not describe.',
                hint=(
                    "every peripheral bus has to be one of the buses the outline "
                    f"lists{_listed(bus_ids)}"
                ),
            )
        channels[peripheral.id] = frozenset(driver.channels)

    for endpoint in outline.endpoints:
        if endpoint.device_type not in registry.DEVICE_TYPES:
            raise _refuse_unknown(
                "device type",
                endpoint.device_type,
                registry.DEVICE_TYPES,
                registry.PLANNED_DEVICE_TYPES,
            )
        for cluster in endpoint.clusters:
            if cluster.cluster not in registry.CLUSTERS:
                raise _refuse_unknown(
                    "cluster", cluster.cluster, registry.CLUSTERS, registry.PLANNED_CLUSTERS
                )
            _check_source(cluster, channels)


def _check_source(cluster: ClusterChoice, channels: Mapping[str, frozenset[str]]) -> None:
    peripheral, _, channel = cluster.source.partition(".")
    known = channels.get(peripheral)
    if known is None:
        raise ConfigError(
            f'"{cluster.cluster}" reads from "{cluster.source}", and the outline '
            f'describes no peripheral called "{peripheral}".',
            hint=(
                "a source is <peripheral>.<channel>, and the peripheral is one the "
                f"outline lists{_listed(channels)}"
            ),
        )
    if channel not in known:
        raise ConfigError(
            f'"{peripheral}" has no channel called "{channel}".',
            hint=f"channels it has: {', '.join(sorted(known))}",
        )


def _transport_lines(board: registry.BoardDef) -> list[str]:
    """The network section for this board's transport, with its defaults."""
    if "thread" in board.transports:
        return [
            "  thread:",
            "    # ftd routes for other nodes and stays awake; mtd/sed sleep and",
            "    # need a mains-powered router nearby. Battery devices want mtd.",
            "    device_role: ftd",
        ]
    if "wifi" in board.transports:  # pragma: no cover - no Wi-Fi board is supported yet
        return [
            "  wifi:",
            "    ssid: !secret wifi_ssid",
            "    password: !secret wifi_password",
        ]
    return []  # pragma: no cover - every supported board has a transport


def _example_lines(board: registry.BoardDef) -> list[str]:
    """The commented hardware and node sections, from real registry rows.

    Every name in here is looked up rather than typed, so the example a
    user uncomments is one that still exists — a hand-written one goes
    stale the first time a driver is renamed, and it goes stale silently,
    in a file the test suite has no reason to read.
    """
    driver = next(iter(registry.DRIVERS.values()))
    cluster = next(iter(registry.CLUSTERS.values()))
    device_type = next(
        entry
        for entry in registry.DEVICE_TYPES.values()
        if cluster.name in entry.mandatory_clusters
    )
    channel = next(
        entry.name for entry in driver.channels.values() if entry.quantity == cluster.quantity
    )
    bus = next((entry for entry in board.buses if entry.kind == driver.bus), None)
    controller = "arduino_i2c" if bus is None else bus.controller

    return [
        "# The hardware this device has, and what it looks like to a controller.",
        "# Both sections are commented out because MCUHome cannot guess what is",
        "# wired up; the example below is a complete, working one. Uncomment it and",
        "# adjust it, or replace it with your own.",
        "#",
        "# hardware:",
        "#   buses:",
        "#     i2c0:",
        f"#       controller: {controller}",
        "#   peripherals:",
        "#     sensor:",
        f"#       driver: {driver.compatible}",
        "#       bus: i2c0",
        "#",
        "# node:",
        "#   endpoints:",
        "#     - id: 1",
        f"#       device_type: {device_type.name}",
        "#       clusters:",
        f"#         {cluster.name}:",
        f"#           source: sensor.{channel}",
        "#           sampling: 30s",
        "#           report:",
        f"#             delta: 0.5    # only report a change of 0.5 {cluster.unit}",
    ]


def _outline_lines(outline: DeviceOutline) -> list[str]:
    """The hardware and node sections a caller described, as real YAML."""
    lines: list[str] = []
    if outline.buses or outline.peripherals:
        lines.append("hardware:")
        if outline.buses:
            lines.append("  buses:")
            for bus in outline.buses:
                lines += [f"    {bus.id}:", f"      controller: {bus.controller}"]
        if outline.peripherals:
            lines.append("  peripherals:")
            for peripheral in outline.peripherals:
                lines += [f"    {peripheral.id}:", f"      driver: {peripheral.driver}"]
                if peripheral.bus is not None:
                    lines.append(f"      bus: {peripheral.bus}")
                if peripheral.address is not None:
                    lines.append(f"      address: 0x{peripheral.address:02x}")
        lines.append("")

    if outline.endpoints:
        lines += ["node:", "  endpoints:"]
        # Numbered from 1 rather than left to the resolver's own
        # numbering: this file is about to be edited by hand, and an
        # endpoint whose id is written down is one a person can move,
        # reorder or point a controller at without changing what it is.
        for number, endpoint in enumerate(outline.endpoints, start=1):
            lines += [
                f"    - id: {number}",
                f"      device_type: {endpoint.device_type}",
            ]
            if endpoint.clusters:
                lines.append("      clusters:")
                for cluster in endpoint.clusters:
                    lines += [
                        f"        {cluster.cluster}:",
                        f"          source: {cluster.source}",
                    ]
                    if cluster.sampling is not None:
                        lines.append(f"          sampling: {cluster.sampling}")
        lines.append("")
    return lines


def render_starter(
    name: str,
    *,
    board: str,
    friendly_name: str | None = None,
    outline: DeviceOutline | None = None,
) -> str:
    """The text of a new device's ``main.yaml``.

    Pure, so the test suite reads it without touching a filesystem and the
    dashboard's new-device wizard can show it before anything is written.
    *friendly_name* is the human-readable name destined for the device's
    Matter identity (cli ADR 0003: ``device new --name``); left unset, a
    title-cased spelling of *name* stands in.

    *outline* is what the caller already knows about the device's
    hardware and endpoints. Left out — or empty — the file carries the
    commented example instead, which is the command line's case.
    """
    definition = registry.BOARDS[board]
    if outline is not None and not outline.is_empty():
        _check_outline(outline)
    else:
        outline = None

    shown_name = friendly_name or name.replace("-", " ").title()
    lines = [
        f"# {name} — an MCUHome device.",
        "#",
        "# This file is yours: MCUHome reads it and never rewrites it, except for",
        "# the one command that writes the commissioning credentials below.",
        "#",
        "# Next step:",
        f"#   mcuhome device matter-pairing --new {name}    # draw its commissioning codes",
        f"#   mcuhome device validate {name}        # see what it resolves to",
        f"#   mcuhome device build {name}           # compile it",
        "",
        "device:",
        f"  name: {name}",
        f"  friendly_name: {json.dumps(shown_name)}",
        f"  board: {board}",
        f'  version: "{ota.DEFAULT_VERSION}"    # raise it for every image you want '
        "a device to update to",
        "  # power:",
        "  #   source: battery    # mains (default) or battery",
        "",
        "network:",
    ]
    lines += _transport_lines(definition)
    lines += [
        "  matter:",
        "    enabled: true",
        "    # The three keys below are this device's commissioning identity.",
        "    # They are drawn once, by mcuhome device matter-pairing --new, and",
        "    # rebuilding never changes them — a device whose identity moved has",
        "    # to be commissioned again. Do not write them by hand.",
        "",
    ]
    lines += _example_lines(definition) if outline is None else _outline_lines(outline)
    if outline is None:
        lines.append("")
    return "\n".join(lines)


def new_device(
    name: str,
    *,
    board: str,
    env: Mapping[str, str],
    cwd: Path,
    project_dir: Path | None = None,
    friendly_name: str | None = None,
    outline: DeviceOutline | None = None,
) -> NewDevice:
    """Create ``devices/<name>/main.yaml``, or refuse and change nothing.

    Refusals come first and cover the five ways this goes wrong: no
    project to create the device in, a name that cannot become a folder
    and a hostname, a board nobody has brought up, an *outline* naming
    something that is not there, and a device that already exists — the
    last one loudly, because overwriting somebody's
    configuration is not a scaffold's business. The project is resolved
    *first* (PO 2026-08-15): "where am I working" is answered before
    the work itself is judged, so a user outside any project hears that
    once, not after every corrected argument.

    The project comes from :func:`mcuhome.workbench.project.resolve_project`'s
    ladder, and outside any project that resolver's refusal already
    points at ``mcuhome project init``: creating a *project* is init's job (ADR
    0022 §1), a device scaffold only ever fills one in. The ``devices/``
    directory itself is created when missing — it is part of the layout
    the marker promises, not a decision.

    *cwd* and *env* are stated rather than read from the process, for
    the reason :func:`mcuhome.workbench.project.resolve_project` gives —
    doubly so here, because this function creates directories.
    """
    project = resolve_project(project_dir, env=env, cwd=cwd)

    if not schema.DEVICE_NAME_RE.match(name) or name.endswith("-"):
        raise _refuse_bad_name(name)
    if len(name) > schema.DEVICE_NAME_MAX:
        raise _refuse_bad_name(name)
    if board not in registry.BOARDS:
        raise _refuse_unknown_board(board)
    # Before the folder is touched: an outline that cannot be rendered is
    # a refusal that leaves nothing behind, not half a device.
    if outline is not None and not outline.is_empty():
        _check_outline(outline)

    entry = project.device_entry(name)
    if entry.exists():
        raise ConfigError(
            f'There is already a device called "{name}" here.',
            location=Location(file=entry),
            hint=(
                "pick another name, or edit the configuration that is already "
                f"there. mcuhome never overwrites a device configuration.\n"
                f"    mcuhome device validate {name}"
            ),
        )

    try:
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_text(
            render_starter(name, board=board, friendly_name=friendly_name, outline=outline),
            encoding="utf-8",
        )
    except OSError as error:
        raise ConfigError(
            f"MCUHome cannot create {entry}: {error.strerror}.",
            hint="pick a writable project location with --project-dir",
        ) from error

    return NewDevice(project=project, entry=entry, name=name, board=board)
