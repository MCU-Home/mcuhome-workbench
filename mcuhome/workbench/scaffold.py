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

__all__ = ["NewDevice", "new_device", "render_starter"]


@dataclass(frozen=True)
class NewDevice:
    """What ``mcuhome new`` created."""

    project: Project
    entry: Path
    name: str
    board: str


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


def render_starter(name: str, *, board: str, friendly_name: str | None = None) -> str:
    """The text of a new device's ``main.yaml``.

    Pure, so the test suite reads it without touching a filesystem and the
    dashboard's new-device wizard can show it before anything is written.
    *friendly_name* is the human-readable name destined for the device's
    Matter identity (cli ADR 0003: ``device new --name``); left unset, a
    title-cased spelling of *name* stands in.
    """
    definition = registry.BOARDS[board]
    example_driver = next(iter(registry.DRIVERS.values()))
    example_cluster = next(iter(registry.CLUSTERS.values()))
    example_type = next(
        entry
        for entry in registry.DEVICE_TYPES.values()
        if example_cluster.name in entry.mandatory_clusters
    )
    example_channel = next(
        channel.name
        for channel in example_driver.channels.values()
        if channel.quantity == example_cluster.quantity
    )

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
        "    # The three keys below are this device's commissioning identity, and",
        "    # they are drawn once, by mcuhome device matter-pairing --new, so every",
        "    # this device is byte-identical. Do not write them by hand.",
        "",
        "# The hardware this device has, and what it looks like to a controller.",
        "# Both sections are commented out because MCUHome cannot guess what is",
        "# wired up; the example below is a complete, working one. Uncomment it and",
        "# adjust it, or replace it with your own.",
        "#",
        "# hardware:",
        "#   buses:",
        "#     i2c0:",
        "#       controller: arduino_i2c",
        "#   peripherals:",
        "#     sensor:",
        f"#       driver: {example_driver.compatible}",
        "#       bus: i2c0",
        "#",
        "# node:",
        "#   endpoints:",
        "#     - id: 1",
        f"#       device_type: {example_type.name}",
        "#       clusters:",
        f"#         {example_cluster.name}:",
        f"#           source: sensor.{example_channel}",
        "#           sampling: 30s",
        "#           report:",
        f"#             delta: 0.5    # only report a change of 0.5 {example_cluster.unit}",
        "",
    ]
    return "\n".join(lines)


def new_device(
    name: str,
    *,
    board: str,
    env: Mapping[str, str],
    cwd: Path,
    project_dir: Path | None = None,
    friendly_name: str | None = None,
) -> NewDevice:
    """Create ``devices/<name>/main.yaml``, or refuse and change nothing.

    Refusals come first and cover the four ways this goes wrong: no
    project to create the device in, a name that cannot become a folder
    and a hostname, a board nobody has brought up, and a device that
    already exists — the last one loudly, because overwriting somebody's
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
            render_starter(name, board=board, friendly_name=friendly_name), encoding="utf-8"
        )
    except OSError as error:
        raise ConfigError(
            f"MCUHome cannot create {entry}: {error.strerror}.",
            hint="pick a writable project location with --project-dir",
        ) from error

    return NewDevice(project=project, entry=entry, name=name, board=board)
