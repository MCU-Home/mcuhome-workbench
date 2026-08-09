# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The registry, as data (dashboard ADR 0011).

Everything MCUHome knows about hardware and about Matter lives in
:mod:`mcuhome.registry` as Python, which nothing that is not Python can
read. This module is the export: the registry as a JSON document a board
picker can populate itself from.

**The payoff is the version-range rule.** Adding a board or a driver in
this repository changes what a dashboard offers, with no dashboard
release — but only if the dashboard reads it as data. That is what makes
the coupling of dashboard ADR 0011 decision 2 a declared range rather
than a pin.

**Deterministic by construction.** Ordering is explicit everywhere (keys
sorted, lists in registry order), no timestamps, no paths, no host facts.
The output is golden-tested, so an accidental change to the shape is a
failing test rather than a surprised consumer.

**The JSON Schema for ``main.yaml`` is next door**
(:mod:`mcuhome.configschema`), and the reason is a dependency rather
than a topic. That export reads :mod:`mcuhome.schema` — the hand-written
parser — for the two rules it must not restate, and the parser is the
front of the build pipeline. The registry export reads the registry and
nothing else, so it can live where the registry lives (ADR 0020); the
schema export cannot follow it there without making the model package
depend on the pipeline that depends on the model.
"""

from __future__ import annotations

import json
from typing import Any

from mcuhome import __version__, registry
from mcuhome.model import MODEL_VERSION

__all__ = [
    "REGISTRY_VERSION",
    "registry_data",
    "to_json",
]

#: Format revision of :func:`registry_data`. Same rule as the build
#: manifest's: additive fields do not bump it.
REGISTRY_VERSION = 1


def to_json(data: dict[str, Any]) -> str:
    """One rendering for both exports, so neither can drift.

    :mod:`mcuhome.configschema` renders with this one; the two documents
    are read by the same editors and served by the same endpoint, and a
    difference in indentation between them would be nobody's decision.
    """
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
        # Whether a commissioned device on this board can be updated over
        # the air (ADR 0015 decision 5). The dashboard needs it to know
        # whether an update it has built is deliverable without a cable, or
        # whether the user has to be told to plug the device in.
        "matter_ota": scheme.matter_ota,
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
