# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""A JSON Schema for ``main.yaml``, generated from the registry and the parser.

Everything MCUHome knows about the shape of a device configuration lives
in :mod:`mcuhome.workbench.schema` as a hand-written parser whose error messages
are user interface. This module exports the *shape* of it — sections,
key names, types, and the enumerations the registry knows — as a JSON
Schema an editor can validate and autocomplete against (dashboard
ADR 0005).

**What it is not: the validator.** Cross-references (does this cluster's
source name a channel this peripheral has?), board capabilities and
Matter conformance are :mod:`mcuhome.workbench.validate`'s work, they need the
whole document at once, and their messages are the reason that module
exists. An editor lints with this; a build validates with the builder.

**Why it is not in :mod:`mcuhome.model.export`,** which exports the registry
the same way and for the same consumer: this one reads
:mod:`mcuhome.workbench.schema` for the name rules it must not restate, and that
parser is the front of the build pipeline (stage 1). The registry export
is registry data rendered as JSON and belongs where the registry does;
this one belongs where the parser does (ADR 0020 draws the line by
execution site, and putting both model-side would make the model package
import the pipeline that imports it).
"""

from __future__ import annotations

from typing import Any

from mcuhome.model import __version__, ota, registry
from mcuhome.model.export import to_json

from mcuhome.workbench import schema

__all__ = [
    "SCHEMA_DIALECT",
    "SCHEMA_ID",
    "config_json_schema",
    "to_json",
]

#: Identifier of the generated JSON Schema. A URL under the documentation
#: domain because that is what ``$id`` is for and what an editor shows;
#: nothing fetches it, and nothing may depend on fetching it.
SCHEMA_ID = "https://docs.mcuhome.org/schema/main.schema.json"

#: JSON Schema dialect. 2020-12 is what the editors of dashboard ADR 0005
#: speak, and the last dialect to change anything relevant here.
SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"


#: Duration, frequency and pin are strings with a unit or a shape in them,
#: because "10s" is what a configuration author writes and 10000 is what
#: they mean. These mirror :mod:`mcuhome.workbench.schema`'s own patterns, spelled
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
                    "version": _string(
                        "Firmware version of this device, as SemVer. It becomes "
                        "MCUboot's image version, the Matter SoftwareVersion a "
                        "controller compares when deciding whether an update is "
                        f"newer, and the version in the .ota file. Defaults to "
                        f"{ota.DEFAULT_VERSION}; each field is at most "
                        f"{ota.VERSION_FIELD_MAX}.",
                        pattern=ota.VERSION_PATTERN,
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
                        "Whether binary blobs may be integrated at all.",
                        enum=["auto", "none"],
                    ),
                    "zephyr_version": _string(
                        'Zephyr release line, "auto" (recommended) or "latest".',
                        pattern=r"^(auto|latest|\d+\.\d+)$",
                    ),
                    "blobs": {
                        "type": "object",
                        "description": "Per-blob opt-in, by blob name.",
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
                            "Commissioning identity. Written by mcuhome device matter-pairing "
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
