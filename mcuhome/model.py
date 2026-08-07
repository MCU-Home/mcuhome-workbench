# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The canonical device model — the builder's one intermediate format.

Stage 3 produces it, every generator consumes it, nothing downstream ever
reads raw YAML (builder-pipeline.md §1.2). It is also the wire format for
remote builds (§6) and the contract the dashboard will consume.

**PROVISIONAL — version 1.** builder-pipeline.md §10 lists
"device-model.json schema versioning" as an open point, to be fixed with
the first dashboard consumption. Until then :data:`MODEL_VERSION` is 1
and the shape may still change; treat a mismatch as "rebuild", never as
something to migrate. What is *not* provisional is the principle behind
the shape: every field exists because a downstream artifact needs it, and
the two runtime contracts decide what those fields mean —

* ``endpoints[]`` mirrors ``<mcuhome/matter_tables.h>`` (ADR 0014): the
  attribute types, sizes, flags and the ``store``/constant distinction
  are that header's, one to one;
* ``channels[]`` mirrors ``<mcuhome/channel.h>``: publish side
  (store, path, period, delta) plus the source binding (device, Zephyr
  sensor channel, integer scale/offset).

Deliberately absent: endpoint 0. The root node is statically compiled
into the framework's own ZAP data model (ADR 0014, decision B) — a
generator that emitted it would collide with it. Also absent: the
Descriptor cluster and the global attributes 0xFFF8..0xFFFD, which the
framework appends and serves itself.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "MODEL_VERSION",
    "AttrModel",
    "BuildModel",
    "BusModel",
    "ChannelModel",
    "ClusterModel",
    "DeviceMeta",
    "DeviceModel",
    "DeviceTypeModel",
    "EndpointModel",
    "HardwareModel",
    "NetworkModel",
    "PairingModel",
    "PeripheralModel",
    "SourceModel",
    "ThreadModel",
    "ToolchainModel",
]

#: Version of the canonical model format. See the provisional note above.
MODEL_VERSION = 1


@dataclass(frozen=True)
class DeviceMeta:
    name: str
    friendly_name: str
    board: str
    power_source: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "friendly_name": self.friendly_name,
            "board": self.board,
            "power_source": self.power_source,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> DeviceMeta:
        return DeviceMeta(
            name=data["name"],
            friendly_name=data["friendly_name"],
            board=data["board"],
            power_source=data["power_source"],
        )


@dataclass(frozen=True)
class ThreadModel:
    device_role: str
    poll_interval_ms: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"device_role": self.device_role, "poll_interval_ms": self.poll_interval_ms}

    @staticmethod
    def from_dict(data: dict[str, Any]) -> ThreadModel:
        return ThreadModel(
            device_role=data["device_role"], poll_interval_ms=data["poll_interval_ms"]
        )


@dataclass(frozen=True)
class PairingModel:
    """This device's commissioning credentials (:mod:`mcuhome.pairing`).

    The SPAKE2+ verifier is deliberately **not** a field: it is a pure
    function of the three values below, and storing it would create a
    second copy that can disagree with them. Everything downstream derives
    it, so a model that travels to a remote build server carries the
    inputs and lets that server compute the same verifier byte for byte.
    """

    discriminator: int
    passcode: int
    #: Base64, exactly as CHIP's Kconfig wants it.
    salt: str
    iterations: int
    #: True when this is the published Matter test tuple, which the
    #: builder is allowed to use only because the configuration asked for
    #: it in so many words (``use_test_pairing: true``).
    test_credentials: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "discriminator": self.discriminator,
            "passcode": self.passcode,
            "salt": self.salt,
            "iterations": self.iterations,
            "test_credentials": self.test_credentials,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> PairingModel:
        return PairingModel(
            discriminator=data["discriminator"],
            passcode=data["passcode"],
            salt=data["salt"],
            iterations=data["iterations"],
            test_credentials=data["test_credentials"],
        )


@dataclass(frozen=True)
class NetworkModel:
    #: ``thread``, ``wifi`` or None for a config without a transport.
    transport: str | None
    matter_enabled: bool
    thread: ThreadModel | None = None
    #: Present exactly when Matter is enabled.
    pairing: PairingModel | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "transport": self.transport,
            "matter_enabled": self.matter_enabled,
            "thread": self.thread.to_dict() if self.thread else None,
            "pairing": self.pairing.to_dict() if self.pairing else None,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> NetworkModel:
        thread = data.get("thread")
        pairing = data.get("pairing")
        return NetworkModel(
            transport=data["transport"],
            matter_enabled=data["matter_enabled"],
            thread=ThreadModel.from_dict(thread) if thread else None,
            pairing=PairingModel.from_dict(pairing) if pairing else None,
        )


@dataclass(frozen=True)
class ToolchainModel:
    zephyr_line: str
    blob_usage: str
    blobs: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "zephyr_line": self.zephyr_line,
            "blob_usage": self.blob_usage,
            "blobs": dict(sorted(self.blobs.items())),
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> ToolchainModel:
        return ToolchainModel(
            zephyr_line=data["zephyr_line"],
            blob_usage=data["blob_usage"],
            blobs=dict(data["blobs"]),
        )


@dataclass(frozen=True)
class BusModel:
    """One bus, ready to become a devicetree node reference."""

    id: str
    kind: str
    #: Board devicetree node the bus maps to (``arduino_i2c``, ``i2c1``…),
    #: or None when the builder has to pick one.
    controller: str | None = None
    sda: str | None = None
    scl: str | None = None
    frequency_hz: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "controller": self.controller,
            "sda": self.sda,
            "scl": self.scl,
            "frequency_hz": self.frequency_hz,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> BusModel:
        return BusModel(
            id=data["id"],
            kind=data["kind"],
            controller=data["controller"],
            sda=data["sda"],
            scl=data["scl"],
            frequency_hz=data["frequency_hz"],
        )


@dataclass(frozen=True)
class PeripheralModel:
    """One devicetree node under a bus."""

    id: str
    #: Devicetree ``compatible`` string.
    compatible: str
    bus: str | None
    #: Devicetree ``reg`` value (the I2C address), or None.
    reg: int | None
    #: Devicetree properties, by their devicetree names.
    properties: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "compatible": self.compatible,
            "bus": self.bus,
            "reg": self.reg,
            "properties": dict(sorted(self.properties.items())),
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> PeripheralModel:
        return PeripheralModel(
            id=data["id"],
            compatible=data["compatible"],
            bus=data["bus"],
            reg=data["reg"],
            properties=dict(data["properties"]),
        )


@dataclass(frozen=True)
class HardwareModel:
    buses: list[BusModel] = field(default_factory=list)
    peripherals: list[PeripheralModel] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "buses": [bus.to_dict() for bus in self.buses],
            "peripherals": [peripheral.to_dict() for peripheral in self.peripherals],
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> HardwareModel:
        return HardwareModel(
            buses=[BusModel.from_dict(item) for item in data["buses"]],
            peripherals=[PeripheralModel.from_dict(item) for item in data["peripherals"]],
        )


@dataclass(frozen=True)
class AttrModel:
    """One entry of ``struct mcuhome_matter_attr``."""

    id: int
    name: str
    type: str
    size: int
    flags: list[str]
    #: Identifier of the RAM cell backing this attribute, or None for a
    #: constant attribute (``store == NULL`` in the generated tables).
    store: str | None
    #: Constant value, and the fallback for an invalid non-nullable cell.
    default: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "size": self.size,
            "flags": list(self.flags),
            "store": self.store,
            "default": self.default,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> AttrModel:
        return AttrModel(
            id=data["id"],
            name=data["name"],
            type=data["type"],
            size=data["size"],
            flags=list(data["flags"]),
            store=data["store"],
            default=data["default"],
        )


@dataclass(frozen=True)
class ClusterModel:
    """One entry of ``struct mcuhome_matter_cluster``."""

    id: int
    name: str
    feature_map: int
    cluster_revision: int
    attrs: list[AttrModel] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "feature_map": self.feature_map,
            "cluster_revision": self.cluster_revision,
            "attrs": [attr.to_dict() for attr in self.attrs],
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> ClusterModel:
        return ClusterModel(
            id=data["id"],
            name=data["name"],
            feature_map=data["feature_map"],
            cluster_revision=data["cluster_revision"],
            attrs=[AttrModel.from_dict(item) for item in data["attrs"]],
        )


@dataclass(frozen=True)
class DeviceTypeModel:
    """One entry of ``struct mcuhome_matter_device_type``."""

    id: int
    revision: int
    name: str

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "revision": self.revision, "name": self.name}

    @staticmethod
    def from_dict(data: dict[str, Any]) -> DeviceTypeModel:
        return DeviceTypeModel(id=data["id"], revision=data["revision"], name=data["name"])


@dataclass(frozen=True)
class EndpointModel:
    """One entry of ``struct mcuhome_matter_endpoint``."""

    id: int
    parent_id: int
    #: Optional user-chosen name, for automations and CoAP paths.
    alias: str | None
    device_types: list[DeviceTypeModel] = field(default_factory=list)
    clusters: list[ClusterModel] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "parent_id": self.parent_id,
            "alias": self.alias,
            "device_types": [device_type.to_dict() for device_type in self.device_types],
            "clusters": [cluster.to_dict() for cluster in self.clusters],
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> EndpointModel:
        return EndpointModel(
            id=data["id"],
            parent_id=data["parent_id"],
            alias=data["alias"],
            device_types=[DeviceTypeModel.from_dict(item) for item in data["device_types"]],
            clusters=[ClusterModel.from_dict(item) for item in data["clusters"]],
        )


@dataclass(frozen=True)
class SourceModel:
    """The produce side: one entry of ``struct mcuhome_sensor_binding``."""

    peripheral: str
    #: Channel name as written in the YAML (``baro.temperature``).
    channel: str
    fetch_channel: str
    zephyr_channel: str
    scale_num: int
    scale_den: int
    offset: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "peripheral": self.peripheral,
            "channel": self.channel,
            "fetch_channel": self.fetch_channel,
            "zephyr_channel": self.zephyr_channel,
            "scale_num": self.scale_num,
            "scale_den": self.scale_den,
            "offset": self.offset,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> SourceModel:
        return SourceModel(
            peripheral=data["peripheral"],
            channel=data["channel"],
            fetch_channel=data["fetch_channel"],
            zephyr_channel=data["zephyr_channel"],
            scale_num=data["scale_num"],
            scale_den=data["scale_den"],
            offset=data["offset"],
        )


@dataclass(frozen=True)
class ChannelModel:
    """One ``struct mcuhome_channel`` plus its source binding."""

    store: str
    type: str
    endpoint_id: int
    cluster_id: int
    attr_id: int
    sample_period_ms: int
    #: Report threshold in the attribute's RAW units.
    report_delta: int
    source: SourceModel

    def to_dict(self) -> dict[str, Any]:
        return {
            "store": self.store,
            "type": self.type,
            "endpoint_id": self.endpoint_id,
            "cluster_id": self.cluster_id,
            "attr_id": self.attr_id,
            "sample_period_ms": self.sample_period_ms,
            "report_delta": self.report_delta,
            "source": self.source.to_dict(),
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> ChannelModel:
        return ChannelModel(
            store=data["store"],
            type=data["type"],
            endpoint_id=data["endpoint_id"],
            cluster_id=data["cluster_id"],
            attr_id=data["attr_id"],
            sample_period_ms=data["sample_period_ms"],
            report_delta=data["report_delta"],
            source=SourceModel.from_dict(data["source"]),
        )


@dataclass(frozen=True)
class BuildModel:
    """What stage 4/5 need beyond the tables: fragments and snippets."""

    snippets: list[str] = field(default_factory=list)
    kconfig: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"snippets": list(self.snippets), "kconfig": list(self.kconfig)}

    @staticmethod
    def from_dict(data: dict[str, Any]) -> BuildModel:
        return BuildModel(snippets=list(data["snippets"]), kconfig=list(data["kconfig"]))


@dataclass(frozen=True)
class DeviceModel:
    """The resolved device: everything a generator needs, nothing else."""

    device: DeviceMeta
    network: NetworkModel
    toolchain: ToolchainModel
    hardware: HardwareModel
    endpoints: list[EndpointModel] = field(default_factory=list)
    channels: list[ChannelModel] = field(default_factory=list)
    build: BuildModel = field(default_factory=BuildModel)
    model_version: int = MODEL_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_version": self.model_version,
            "device": self.device.to_dict(),
            "network": self.network.to_dict(),
            "toolchain": self.toolchain.to_dict(),
            "hardware": self.hardware.to_dict(),
            "endpoints": [endpoint.to_dict() for endpoint in self.endpoints],
            "channels": [channel.to_dict() for channel in self.channels],
            "build": self.build.to_dict(),
        }

    def to_json(self) -> str:
        """Serialize deterministically (golden-file testable, §9)."""
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n"

    @staticmethod
    def from_dict(data: dict[str, Any]) -> DeviceModel:
        return DeviceModel(
            model_version=data["model_version"],
            device=DeviceMeta.from_dict(data["device"]),
            network=NetworkModel.from_dict(data["network"]),
            toolchain=ToolchainModel.from_dict(data["toolchain"]),
            hardware=HardwareModel.from_dict(data["hardware"]),
            endpoints=[EndpointModel.from_dict(item) for item in data["endpoints"]],
            channels=[ChannelModel.from_dict(item) for item in data["channels"]],
            build=BuildModel.from_dict(data["build"]),
        )

    @staticmethod
    def from_json(text: str) -> DeviceModel:
        return DeviceModel.from_dict(json.loads(text))
