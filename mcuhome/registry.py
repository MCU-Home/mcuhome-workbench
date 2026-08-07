# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Static knowledge tables: Matter clusters, device types, drivers, boards.

This module is the builder's only source of "what does Matter require
here" and "what hardware do we know". It is deliberately *data*, not
logic — new support is a table row, exactly like the runtime side
(builder-pipeline.md §1).

**Sourcing rule (from samples/matter-node/src/mcuhome_config.c).** Every
cluster and device-type revision comes from CHIP v1.5.1.0's own
*implementation* data model, because that is what CHIP-based nodes report
in the field:

* cluster revisions — ``src/app/zap-templates/zcl/data-model/chip/
  *-cluster.xml``, cross-checked against ``src/controller/data_model/
  controller-clusters.matter``;
* device-type revisions — ``.../data-model/chip/matter-devices.xml``.

The spec scrape shipped alongside (``data_model/1.5.1/``) is one revision
ahead for everything in this file; those are the numbers a future CHIP
release grows into, not the ones its current code implements. Re-check on
every SDK bump — and note that this table exists precisely so the check
is one diff, not an audit of the generator.

**Scope.** Seeded with exactly what MCUHome v0.1 needs (the hardware-
verified BMP180 two-endpoint node). Everything the schema design promises
but the runtime cannot serve yet is listed in the ``PLANNED_*`` sets, so
the validator can say "not supported yet" instead of "unknown" — a very
different message for a user reading the design docs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction

__all__ = [
    "ATTR_SIZES",
    "BOARDS",
    "CLUSTERS",
    "DEVICE_TYPES",
    "DRIVERS",
    "PLANNED_BOARDS",
    "PLANNED_CLUSTERS",
    "PLANNED_DEVICE_TYPES",
    "PLANNED_DRIVERS",
    "AttrDef",
    "BoardDef",
    "ClusterDef",
    "DeviceTypeDef",
    "DriverChannelDef",
    "DriverDef",
]

# --------------------------------------------------------------------------
# Attribute primitives (mirror <mcuhome/matter_tables.h>)
# --------------------------------------------------------------------------

#: Wire size in bytes per attribute type name. The names are the lowercase
#: form of ``enum mcuhome_attr_type`` and travel through the canonical
#: model to the code generator, which maps them back to
#: ``MCUHOME_ATTR_TYPE_*``.
ATTR_SIZES: dict[str, int] = {
    "int16s": 2,
    "int16u": 2,
    "int32s": 4,
    "int32u": 4,
    "bool": 1,
}

#: Attribute flag names; mirror ``MCUHOME_ATTR_F_*``.
ATTR_FLAGS = ("writable", "nullable")


@dataclass(frozen=True)
class AttrDef:
    """One attribute a cluster always has.

    ``role`` tells the resolver where the value comes from:

    ``measured_value``
        RAM-backed (``store``), fed by a channel, nullable — a sensor
        without a reading reports null, never a plausible zero.
    ``min_measured_value`` / ``max_measured_value``
        Constant (``store == NULL`` in the generated tables), taken from
        the cluster's ``range:`` in the YAML.
    """

    id: int
    name: str
    role: str
    type: str
    nullable: bool = False
    writable: bool = False


@dataclass(frozen=True)
class ClusterDef:
    """One Matter server cluster MCUHome can generate."""

    name: str
    id: int
    revision: int
    feature_map: int
    #: Physical quantity the cluster measures. A cluster only accepts a
    #: peripheral channel of the same quantity (component-model.md §2).
    quantity: str
    #: Unit the *YAML* speaks in (``report.delta``, ``range``).
    unit: str
    #: YAML unit -> Matter raw attribute unit.
    raw_per_unit: Fraction
    #: Zephyr sensor-API unit -> Matter raw attribute unit, as the
    #: ``scale_num``/``scale_den`` pair of ``struct mcuhome_sensor_binding``.
    zephyr_scale: tuple[int, int]
    #: Fallback for ``range:`` when the config does not give one, in
    #: ``unit``. Deliberately broad; a config should narrow it to the
    #: part's datasheet range, which is what a controller shows as the
    #: sensor's operating range.
    default_range: tuple[float, float]
    attrs: tuple[AttrDef, ...]

    @property
    def measured_attr(self) -> AttrDef:
        return next(attr for attr in self.attrs if attr.role == "measured_value")


_MEASUREMENT_ATTRS = (
    AttrDef(id=0x0000, name="measured_value", role="measured_value", type="int16s", nullable=True),
    AttrDef(id=0x0001, name="min_measured_value", role="min_measured_value", type="int16s"),
    AttrDef(id=0x0002, name="max_measured_value", role="max_measured_value", type="int16s"),
)

CLUSTERS: dict[str, ClusterDef] = {
    # TemperatureMeasurement, ClusterRevision 4 (CHIP v1.5.1.0:
    # controller-clusters.matter). MeasuredValue is 0.01 °C.
    # FeatureMap 0: the cluster defines no features.
    "temperature_measurement": ClusterDef(
        name="temperature_measurement",
        id=0x0402,
        revision=4,
        feature_map=0,
        quantity="temperature",
        unit="°C",
        raw_per_unit=Fraction(100),
        zephyr_scale=(100, 1),  # Zephyr sensor API reports °C
        default_range=(-40.0, 125.0),
        attrs=_MEASUREMENT_ATTRS,
    ),
    # PressureMeasurement, ClusterRevision 3 (CHIP v1.5.1.0).
    # MeasuredValue is 0.1 kPa == 1 hPa == 1 mbar (Matter Application
    # Cluster Specification §2.4: MeasuredValue = 10 x Pressure[kPa]);
    # the unit prose is not in CHIP's XML, which carries types and
    # conformance only. FeatureMap 0: the only feature is EXT (extended
    # range/resolution), which would make ScaledValue/Scale mandatory —
    # more resolution than these parts produce.
    "pressure_measurement": ClusterDef(
        name="pressure_measurement",
        id=0x0403,
        revision=3,
        feature_map=0,
        quantity="pressure",
        unit="hPa",
        raw_per_unit=Fraction(1),
        zephyr_scale=(10, 1),  # Zephyr sensor API reports kPa
        default_range=(300.0, 1100.0),
        attrs=_MEASUREMENT_ATTRS,
    ),
}

#: Clusters the schema design promises (yaml-schema.md §6) but v0.1 has no
#: runtime for. Named here so the error says "not supported yet".
PLANNED_CLUSTERS: dict[str, str] = {
    "relative_humidity_measurement": "humidity support lands with the next sensor components",
    "illuminance_measurement": "light sensing lands with the next sensor components",
    "carbon_dioxide_measurement": "air-quality sensing lands with the next sensor components",
    "on_off": "actuator clusters need the write path, which the channel contract v1 does not have",
    "level_control": (
        "actuator clusters need the write path, which the channel contract v1 does not have"
    ),
}


# --------------------------------------------------------------------------
# Device types
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DeviceTypeDef:
    """One Matter device type, with the clusters it makes mandatory."""

    name: str
    id: int
    revision: int
    #: Server clusters the device type requires, beyond the Descriptor
    #: cluster the framework appends itself (ADR 0014, decision B).
    mandatory_clusters: tuple[str, ...]


DEVICE_TYPES: dict[str, DeviceTypeDef] = {
    # Temperature Sensor, revision 3 (matter-devices.xml:1849).
    "temperature_sensor": DeviceTypeDef(
        name="temperature_sensor",
        id=0x0302,
        revision=3,
        mandatory_clusters=("temperature_measurement",),
    ),
    # Pressure Sensor, revision 2 (matter-devices.xml; the spec scrape
    # says 3 — see the sourcing rule at the top of this file).
    "pressure_sensor": DeviceTypeDef(
        name="pressure_sensor",
        id=0x0305,
        revision=2,
        mandatory_clusters=("pressure_measurement",),
    ),
}

PLANNED_DEVICE_TYPES: dict[str, str] = {
    "humidity_sensor": "needs the relative_humidity_measurement cluster",
    "light_sensor": "needs the illuminance_measurement cluster",
    "air_quality_sensor": "needs the air-quality cluster set",
    "contact_sensor": "needs the boolean_state cluster",
    "on_off_light": "needs the on_off cluster and the actuator write path",
    "on_off_plug_in_unit": "needs the on_off cluster and the actuator write path",
}


# --------------------------------------------------------------------------
# Peripheral drivers
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DriverChannelDef:
    """One value stream a peripheral produces (yaml-schema.md §5)."""

    name: str
    quantity: str
    #: Channel passed to ``sensor_channel_get()``.
    zephyr_channel: str
    #: Channel passed to ``sensor_sample_fetch_chan()``. Usually
    #: ``SENSOR_CHAN_ALL``: many Zephyr drivers assert on anything else.
    fetch_channel: str = "SENSOR_CHAN_ALL"


@dataclass(frozen=True)
class DriverDef:
    """One supported ``hardware.peripherals[].driver`` value."""

    #: Devicetree compatible string, verbatim (yaml-schema.md §5).
    compatible: str
    #: Bus kind the peripheral sits on, or None for a bus-less peripheral.
    bus: str | None
    channels: dict[str, DriverChannelDef]
    #: Devicetree properties accepted under the peripheral, by DT name and
    #: Python type. Written verbatim as in the binding, so the overlay
    #: generator is a copy and the YAML stays devicetree-aligned.
    properties: dict[str, type] = field(default_factory=dict)
    #: Kconfig symbols the driver needs *on top of* what devicetree
    #: implies. Normally empty: Zephyr sensor drivers default to ``y``
    #: from their DT node ("hardware in DTS, never in Kconfig" —
    #: samples/matter-node/prj.conf).
    kconfig: tuple[str, ...] = ()
    #: Only address this chip can have, when it has no address pins.
    fixed_address: int | None = None


DRIVERS: dict[str, DriverDef] = {
    # Bosch BMP180 — the part the phase-1 node was verified with.
    # Zephyr driver: drivers/sensor/bosch/bmp180/. It reports die
    # temperature (SENSOR_CHAN_DIE_TEMP), which in a BMP180 *is* the
    # reference thermometer for the pressure compensation and tracks
    # ambient closely enough for a ±2 °C part.
    "bosch,bmp180": DriverDef(
        compatible="bosch,bmp180",
        bus="i2c",
        channels={
            "temperature": DriverChannelDef(
                name="temperature",
                quantity="temperature",
                zephyr_channel="SENSOR_CHAN_DIE_TEMP",
            ),
            "pressure": DriverChannelDef(
                name="pressure",
                quantity="pressure",
                zephyr_channel="SENSOR_CHAN_PRESS",
            ),
        },
        # osr-press: pressure oversampling, 0..3 (dts/bindings/sensor/
        # bosch,bmp180.yaml in Zephyr).
        properties={"osr-press": int},
        # CONFIG_SENSOR is added by the resolver for any sensor
        # peripheral; CONFIG_BMP180 defaults to y from the DT node.
        kconfig=(),
        fixed_address=0x77,
    ),
}

PLANNED_DRIVERS: dict[str, str] = {
    "sensirion,sht4x": "temperature/humidity sensors land with the next component batch",
    "sensirion,scd41": "CO2 sensors land with the air-quality component batch",
    "bosch,bme680": "the BME680/BME688 component lands with the next component batch",
    "rohm,bh1750": "light sensors land with the next component batch",
    "gpio-led": "actuator components need the channel write path",
    "gpio-output": "actuator components need the channel write path",
    "gpio-key": "input components need the event path",
    "voltage-divider": "battery monitoring lands with the power-management phase",
}


# --------------------------------------------------------------------------
# Boards
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class BoardDef:
    """One Zephyr board target MCUHome can build for.

    The design's end state is "validate against the boards known to the
    pinned Zephyr" (yaml-schema.md §3). Until the builder container can
    enumerate them, the honest list is the one MCUHome has actually
    brought up — a config for an unlisted board would compile into
    something nobody has ever run.
    """

    name: str
    #: Transports the board can serve today.
    transports: frozenset[str]
    #: Board-scoped Kconfig, from the verified board configuration
    #: (samples/matter-node/boards/*.conf).
    kconfig: tuple[str, ...] = ()
    #: Board-scoped devicetree, emitted above the peripherals every device
    #: configuration describes for itself. This is board *wiring* — true of
    #: every MCUHome device on this board, never of one configuration — so
    #: it belongs to the board, exactly like ``kconfig`` above. Verbatim
    #: devicetree; the generator only indents nothing and adds no syntax.
    overlay: str = ""
    #: Why :attr:`overlay` is there, rendered as its comment in the
    #: generated file. Generic knowledge about the board, per the
    #: generated-comments rule in :mod:`mcuhome.generate`.
    overlay_note: str = ""


#: Board wiring of the nRF5340 application core: it has no RNG peripheral
#: of its own. See :data:`BOARDS`.
_NRF5340_ENTROPY_NOTE = (
    "Entropy. The nRF5340 application core has no RNG peripheral: the one on the die "
    "belongs to the network core. The board default points zephyr,entropy at the "
    "Bluetooth HCI entropy driver, which cannot work here because MCUHome runs this "
    "SoC Thread-only with Bluetooth off (ADR 0011). Redirect to the framework's IPC "
    "driver, which seeds a CTR-DRBG from the network core's real RNG over a second "
    "endpoint on the ipc0 instance the 802.15.4 spinel channel already uses.\n"
    "The network core must run mcuhome/samples/netcore-radio for this endpoint to "
    "exist; against the upstream 802154_rpmsg image the node comes up and then fails "
    "every entropy request with -EIO rather than falling back to a weaker generator."
)

_NRF5340_ENTROPY_OVERLAY = """\
/ {
\tnetcore_entropy: netcore-entropy {
\t\tcompatible = "mcuhome,entropy-ipc";
\t\tipc = <&ipc0>;
\t\tstatus = "okay";
\t};

\tchosen {
\t\tzephyr,entropy = &netcore_entropy;
\t};
};"""


BOARDS: dict[str, BoardDef] = {
    # nRF7002-DK, application core. Thread-only: BLE is deliberately off
    # (ADR 0011 — vanilla Zephyr has no combined BLE + 802.15.4 netcore
    # image), and the app core has no RNG peripheral, so entropy comes
    # from the network core over IPC.
    "nrf7002dk/nrf5340/cpuapp": BoardDef(
        name="nrf7002dk/nrf5340/cpuapp",
        transports=frozenset({"thread"}),
        kconfig=("CONFIG_BT=n", "CONFIG_ENTROPY_GENERATOR=y"),
        overlay=_NRF5340_ENTROPY_OVERLAY,
        overlay_note=_NRF5340_ENTROPY_NOTE,
    ),
}

PLANNED_BOARDS: dict[str, str] = {
    "nrf52840dk/nrf52840": "not brought up yet",
    "nrf52840dongle/nrf52840": "brought up as a bare Matter node, but has no I2C bus broken out",
    "nrf54l15dk/nrf54l15/cpuapp": "not brought up yet",
}
