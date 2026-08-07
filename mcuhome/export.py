# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The registry and the YAML schema, as data (dashboard ADR 0011).

Everything MCUHome knows about hardware and about Matter lives in
:mod:`mcuhome.registry` as Python, and everything it knows about the
shape of ``main.yaml`` lives in :mod:`mcuhome.schema` as a hand-written
parser whose error messages are user interface. Both are the firmware
repository's contract to own (dashboard AGENTS.md), and neither is
readable by anything that is not Python. This module is the export: the
registry as a JSON document a board picker can populate itself from, and
a JSON Schema an editor can validate and autocomplete against.

**The payoff is the version-range rule.** Adding a board or a driver in
this repository changes what a dashboard offers, with no dashboard
release — but only if the dashboard reads it as data. That is what makes
the coupling of dashboard ADR 0011 decision 2 a declared range rather
than a pin.

**Deterministic by construction.** Ordering is explicit everywhere (keys
sorted, lists in registry order), no timestamps, no paths, no host facts.
The output is golden-tested, so an accidental change to the shape is a
failing test rather than a surprised consumer.

**What the JSON Schema is and is not.** It is the *shape* of a
configuration: sections, key names, types, and the enumerations the
registry knows — enough for an editor to complete a board name and to
underline a misspelled cluster. It is deliberately not the validator:
cross-references (does this cluster's source name a channel this
peripheral has?), board capabilities and Matter conformance are
:mod:`mcuhome.validate`'s work, they need the whole document at once, and
their messages are the reason that module exists. An editor lints with
this; a build validates with the builder.
"""

from __future__ import annotations

import json
from typing import Any

from mcuhome import __version__, registry, schema
from mcuhome.model import MODEL_VERSION

__all__ = [
    "REGISTRY_VERSION",
    "SCHEMA_ID",
    "config_json_schema",
    "registry_data",
    "to_json",
]

#: Format revision of :func:`registry_data`. Same rule as the build
#: manifest's: additive fields do not bump it.
REGISTRY_VERSION = 1

#: Identifier of the generated JSON Schema. A URL under the documentation
#: domain because that is what ``$id`` is for and what an editor shows;
#: nothing fetches it, and nothing may depend on fetching it.
SCHEMA_ID = "https://docs.mcuhome.org/schema/main.schema.json"

#: JSON Schema dialect. 2020-12 is what the editors of dashboard ADR 0005
#: speak, and the last dialect to change anything relevant here.
SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"


def to_json(data: dict[str, Any]) -> str:
    """One rendering for both exports, so neither can drift."""
    return json.dumps(data, indent=2) + "\n"


# --------------------------------------------------------------------------
# The registry
# --------------------------------------------------------------------------


def _partition(entry: registry.PartitionDef) -> dict[str, Any]:
    return {
        "label": entry.label,
        "fixed_label": entry.fixed_label,
        "device": entry.device,
        "offset": entry.offset,
        "size": entry.size,
    }


def _update_scheme(scheme: registry.UpdateSchemeDef) -> dict[str, Any]:
    return {
        "board_class": scheme.board_class,
        "mcuboot_mode": scheme.mcuboot_mode,
        "staging": scheme.staging,
        "recovery": list(scheme.recovery),
        "erase_block_size": scheme.erase_block_size,
        "write_block_size": scheme.write_block_size,
        "header_size": scheme.header_size,
        "signature_type": registry.SIGNATURE_TYPE,
        "partitions": [_partition(entry) for entry in scheme.partitions],
        "application_snippets": list(scheme.application_snippets),
        "bootloader_snippets": list(scheme.bootloader_snippets),
    }


def _bootstrap(bootstrap: registry.BootstrapDef) -> dict[str, Any]:
    return {
        "mechanism": bootstrap.mechanism,
        "state": bootstrap.state,
        "artifact": bootstrap.artifact,
        "steps": list(bootstrap.steps),
    }


def _board(board: registry.BoardDef) -> dict[str, Any]:
    return {
        "name": board.name,
        "transports": sorted(board.transports),
        "update_scheme": None
        if board.update_scheme is None
        else _update_scheme(board.update_scheme),
        "bootstrap": None if board.bootstrap is None else _bootstrap(board.bootstrap),
    }


def _driver(driver: registry.DriverDef) -> dict[str, Any]:
    return {
        # `compatible` and not `driver`: it is the devicetree compatible
        # string verbatim (yaml-schema.md §5), it is what the YAML key
        # `driver:` holds, and it is what the canonical model carries.
        # One name for one thing, all the way through.
        "compatible": driver.compatible,
        "bus": driver.bus,
        "fixed_address": driver.fixed_address,
        "channels": [
            {"name": channel.name, "quantity": channel.quantity}
            for channel in driver.channels.values()
        ],
        "properties": {name: kind.__name__ for name, kind in sorted(driver.properties.items())},
        "kconfig": list(driver.kconfig),
    }


def _cluster(cluster: registry.ClusterDef) -> dict[str, Any]:
    return {
        "name": cluster.name,
        "id": cluster.id,
        "revision": cluster.revision,
        "feature_map": cluster.feature_map,
        "quantity": cluster.quantity,
        "unit": cluster.unit,
        # A fraction, exactly: the YAML speaks °C and the attribute
        # carries hundredths of one, and a consumer converting a report
        # delta needs that ratio without a rounding step in between.
        "raw_per_unit": [cluster.raw_per_unit.numerator, cluster.raw_per_unit.denominator],
        "default_range": list(cluster.default_range),
        "attributes": [
            {
                "id": attr.id,
                "name": attr.name,
                "role": attr.role,
                "type": attr.type,
                "nullable": attr.nullable,
                "writable": attr.writable,
            }
            for attr in cluster.attrs
        ],
    }


def _device_type(device_type: registry.DeviceTypeDef) -> dict[str, Any]:
    return {
        "name": device_type.name,
        "id": device_type.id,
        "revision": device_type.revision,
        "mandatory_clusters": list(device_type.mandatory_clusters),
    }


def _planned(entries: dict[str, str]) -> list[dict[str, str]]:
    """The "not supported yet" tables, with their reasons.

    Exported rather than hidden: a picker that shows what is coming, and
    says why it is not there, is a better answer than a short list with
    no explanation — and it is the same message the validator gives.
    """
    return [{"name": name, "reason": reason} for name, reason in sorted(entries.items())]


def registry_data() -> dict[str, Any]:
    """Everything the builder knows about hardware and Matter, as JSON."""
    return {
        "registry_version": REGISTRY_VERSION,
        "builder_version": __version__,
        "model_version": MODEL_VERSION,
        "boards": [_board(board) for board in registry.BOARDS.values()],
        "planned_boards": _planned(registry.PLANNED_BOARDS),
        "drivers": [_driver(driver) for driver in registry.DRIVERS.values()],
        "planned_drivers": _planned(registry.PLANNED_DRIVERS),
        "clusters": [_cluster(cluster) for cluster in registry.CLUSTERS.values()],
        "planned_clusters": _planned(registry.PLANNED_CLUSTERS),
        "device_types": [_device_type(entry) for entry in registry.DEVICE_TYPES.values()],
        "planned_device_types": _planned(registry.PLANNED_DEVICE_TYPES),
        "attribute_sizes": dict(sorted(registry.ATTR_SIZES.items())),
    }


# --------------------------------------------------------------------------
# The YAML schema
# --------------------------------------------------------------------------

#: Duration, frequency and pin are strings with a unit or a shape in them,
#: because "10s" is what a configuration author writes and 10000 is what
#: they mean. These mirror :mod:`mcuhome.schema`'s own patterns, spelled
#: for JSON Schema — which has no case-insensitive flag, so the frequency
#: unit is written out in both cases rather than borrowed verbatim. A
#: test asserts the two agree on a table of strings, because a pattern
#: that drifts turns an editor into a liar.
_DURATION_PATTERN = r"^(\d+(?:\.\d+)?)\s*(ms|s|min|h|d)$"
_FREQUENCY_PATTERN = r"^(\d+(?:\.\d+)?)\s*([kKmM]?[hH][zZ])$"
_PIN_PATTERN = r"^gpio\d+\.\d+$"


def _string(description: str, **extra: Any) -> dict[str, Any]:
    return {"type": "string", "description": description, **extra}


def _cluster_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "description": (
            "One Matter server cluster on this endpoint, keyed by cluster name. "
            "The value says where its measured value comes from and how often."
        ),
        "additionalProperties": False,
        "properties": {
            "source": _string(
                "Channel that feeds this cluster, as <peripheral>.<channel>, e.g. baro.temperature."
            ),
            "target": _string("Channel this cluster writes to (actuators; not in v0.1 yet)."),
            "sampling": _string(
                "How often the source is read, as a duration: 10s, 500ms, 5min.",
                pattern=_DURATION_PATTERN,
            ),
            "report": {
                "type": "object",
                "description": "When a new reading is reported to the controller.",
                "additionalProperties": False,
                "properties": {
                    "delta": {
                        "type": "number",
                        "description": (
                            "Report only when the value moved by at least this much, "
                            "in the cluster's own unit."
                        ),
                    },
                    "max_interval": _string(
                        "Report at least this often even without a change.",
                        pattern=_DURATION_PATTERN,
                    ),
                },
            },
            "range": {
                "type": "object",
                "description": (
                    "Operating range a controller shows for this sensor, in the "
                    "cluster's own unit. Defaults to the cluster's broad range; "
                    "narrow it to the part's datasheet."
                ),
                "additionalProperties": False,
                "required": ["min", "max"],
                "properties": {
                    "min": {"type": "number"},
                    "max": {"type": "number"},
                },
            },
        },
    }


def _endpoint_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "description": "One Matter endpoint: a device type and the clusters serving it.",
        "additionalProperties": False,
        "required": ["device_type"],
        "properties": {
            "id": {
                "type": "integer",
                "minimum": 1,
                "description": (
                    "Endpoint number. Endpoint 0 is the root node and is synthesized "
                    "by the framework; devices start at 1."
                ),
            },
            "alias": _string("Name for this endpoint in generated code and in the summary."),
            "device_type": _string(
                "Matter device type this endpoint implements.",
                enum=sorted(registry.DEVICE_TYPES),
            ),
            "clusters": {
                "type": "object",
                "description": "Server clusters on this endpoint, by cluster name.",
                "additionalProperties": False,
                "propertyNames": {"enum": sorted(registry.CLUSTERS)},
                "properties": {name: _cluster_schema() for name in sorted(registry.CLUSTERS)},
            },
        },
    }


def config_json_schema() -> dict[str, Any]:
    """A JSON Schema for ``devices/<name>/main.yaml``.

    Editor validation and autocomplete (dashboard ADR 0005). See the
    module docstring for what this deliberately does not check.
    """
    return {
        "$schema": SCHEMA_DIALECT,
        "$id": SCHEMA_ID,
        "title": "MCUHome device configuration",
        "description": (
            "One MCUHome device, as devices/<name>/main.yaml. Generated from the "
            f"MCUHome builder {__version__}; the builder is the authority, this "
            "file is its shape."
        ),
        "type": "object",
        "required": ["device"],
        "additionalProperties": False,
        "properties": {
            "device": {
                "type": "object",
                "description": "Who this device is and what it runs on.",
                "additionalProperties": False,
                "required": ["name", "board"],
                "properties": {
                    "name": _string(
                        "Device name, lowercase letters, digits and dashes; it "
                        "becomes the node's hostname.",
                        pattern=schema.DEVICE_NAME_RE.pattern,
                        maxLength=schema.DEVICE_NAME_MAX,
                    ),
                    "friendly_name": _string("Name a controller shows to a human."),
                    "board": _string(
                        "Zephyr board target, verbatim.",
                        enum=sorted(registry.BOARDS),
                    ),
                    "power": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "source": _string(
                                "What powers the device; battery selects the "
                                "low-power transport defaults.",
                                enum=["battery", "mains"],
                            )
                        },
                    },
                    "blob_usage": _string(
                        "Whether binary blobs may be integrated at all (ADR 0013).",
                        enum=["auto", "none"],
                    ),
                    "zephyr_version": _string(
                        'Zephyr release line, "auto" (recommended) or "latest".',
                        pattern=r"^(auto|latest|\d+\.\d+)$",
                    ),
                    "blobs": {
                        "type": "object",
                        "description": "Per-blob opt-in, by blob name (ADR 0013).",
                        "additionalProperties": {"enum": ["enabled", "disabled", "auto"]},
                    },
                },
            },
            "network": {
                "type": "object",
                "description": "Transport and Matter. A device without it is standalone.",
                "additionalProperties": False,
                "properties": {
                    "thread": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "device_role": _string(
                                "Thread role. ftd routes, mtd/sed/ssed sleep.",
                                enum=["ftd", "mtd", "sed", "ssed"],
                            ),
                            "poll_interval": _string(
                                "How often a sleepy device polls its parent.",
                                pattern=_DURATION_PATTERN,
                            ),
                        },
                    },
                    "wifi": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "ssid": _string("Network name."),
                            "password": _string(
                                "Network password. Use !secret rather than a literal."
                            ),
                        },
                    },
                    "matter": {
                        "type": "object",
                        "description": (
                            "Commissioning identity. Written by mcuhome init-pairing "
                            "— drawn once, so that every build of this device is "
                            "byte-identical."
                        ),
                        "additionalProperties": False,
                        "properties": {
                            "enabled": {"type": "boolean"},
                            "discriminator": {
                                "type": "integer",
                                "minimum": 0,
                                "maximum": 0xFFF,
                            },
                            "passcode": {"type": "integer"},
                            "salt": _string("Base64 SPAKE2+ salt."),
                            "use_test_pairing": {
                                "type": "boolean",
                                "description": (
                                    "Use the credentials published with the Matter SDK. "
                                    "Bench use only: anyone who knows them can "
                                    "commission the device."
                                ),
                            },
                        },
                    },
                },
            },
            "hardware": {
                "type": "object",
                "description": "Buses and the peripherals on them.",
                "additionalProperties": False,
                "properties": {
                    "buses": {
                        "type": "object",
                        "description": "Buses this device uses, by id (e.g. i2c0).",
                        "additionalProperties": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "controller": _string(
                                    "Board connector or devicetree node label the bus "
                                    "is on, e.g. arduino_i2c."
                                ),
                                "sda": _string(
                                    "Data pin, as gpio<port>.<pin>.", pattern=_PIN_PATTERN
                                ),
                                "scl": _string(
                                    "Clock pin, as gpio<port>.<pin>.", pattern=_PIN_PATTERN
                                ),
                                "frequency": _string(
                                    "Bus frequency, e.g. 400kHz.", pattern=_FREQUENCY_PATTERN
                                ),
                            },
                        },
                    },
                    "peripherals": {
                        "type": "object",
                        "description": "Peripherals, by the id the clusters refer to.",
                        "additionalProperties": {
                            "type": "object",
                            "required": ["driver"],
                            # Driver properties (osr-press and the like) are
                            # per-driver devicetree properties, checked against
                            # the driver's own table once the driver is known —
                            # which a JSON Schema cannot express and the
                            # validator does precisely.
                            "additionalProperties": True,
                            "properties": {
                                "driver": _string(
                                    "Devicetree compatible string of the part.",
                                    enum=sorted(registry.DRIVERS),
                                ),
                                "bus": _string("Id of the bus above that this part sits on."),
                                "address": {
                                    "type": "integer",
                                    "minimum": 0,
                                    "description": "Bus address, where the part has one.",
                                },
                            },
                        },
                    },
                },
            },
            "node": {
                "type": "object",
                "description": "What this device looks like to a Matter controller.",
                "additionalProperties": False,
                "required": ["endpoints"],
                "properties": {
                    "endpoints": {
                        "type": "array",
                        "minItems": 1,
                        "items": _endpoint_schema(),
                    }
                },
            },
            "automations": {
                "type": "array",
                "description": "Automations. Not implemented in v0.1.",
                "items": {"type": "object"},
            },
        },
    }
