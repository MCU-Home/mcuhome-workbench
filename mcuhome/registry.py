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
    "MCUBOOT_MODE_SYMBOLS",
    "PLANNED_BOARDS",
    "PLANNED_CLUSTERS",
    "PLANNED_DEVICE_TYPES",
    "PLANNED_DRIVERS",
    "SIGNATURE_TYPE",
    "AttrDef",
    "BoardDef",
    "BootstrapDef",
    "ClusterDef",
    "DeviceTypeDef",
    "DriverChannelDef",
    "DriverDef",
    "PartitionDef",
    "UpdateSchemeDef",
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
# Update scheme and flash layout (ADR 0015)
# --------------------------------------------------------------------------

#: Sysbuild symbol per MCUboot mode name. The names on the left are what
#: :class:`UpdateSchemeDef` speaks; the symbols on the right are what
#: ``sysbuild.conf`` carries. A board names a mode, never a symbol —
#: nothing in the builder may branch on a board (ADR 0015 decision 2).
MCUBOOT_MODE_SYMBOLS: dict[str, str] = {
    "single-app": "SB_CONFIG_MCUBOOT_MODE_SINGLE_APP",
    "swap-using-offset": "SB_CONFIG_MCUBOOT_MODE_SWAP_USING_OFFSET",
}

#: The one signature scheme MCUHome images use (ADR 0015 decision 8), as
#: the name a manifest and a schema export speak. The sysbuild symbol it
#: renders to is ``SB_CONFIG_BOOT_SIGNATURE_TYPE_ECDSA_P256``. It is a
#: constant rather than per-board data because a device that needed a
#: different one would need a different key custody story with it — that
#: re-opens ADR 0015, which is exactly what a registry row must not do.
SIGNATURE_TYPE = "ecdsa-p256"

#: MCUboot modes whose upgrade slot is the *secondary* one, and which
#: therefore size an image against ``slot1_partition``. Zephyr's
#: ``cmake/mcuboot.cmake`` makes the same distinction to pick imgtool's
#: ``--slot-size``; :attr:`UpdateSchemeDef.imgtool_slot` mirrors it so the
#: build manifest can state the parameter without a build having run.
_SECONDARY_SLOT_MODES = frozenset({"swap-using-offset"})


@dataclass(frozen=True)
class PartitionDef:
    """One region of the board's flash, as the builder fixes it.

    Sizes and offsets are the ADR 0015 layout tables, not the board's
    upstream defaults — those cannot hold an MCUHome image (ADR 0015,
    Consequences). :attr:`device` says which part it lives on.
    """

    #: Devicetree node label, e.g. ``slot0_partition``.
    label: str
    #: The ``label`` property inside the node, e.g. ``image-0``. This is
    #: what ``FIXED_PARTITION_ID()`` and MCUboot's flash map resolve.
    fixed_label: str
    offset: int
    size: int
    #: Devicetree node label of the flash part, or ``None`` for the SoC's
    #: internal flash.
    device: str | None = None

    @property
    def end(self) -> int:
        return self.offset + self.size

    def describe(self) -> str:
        where = self.device or "internal"
        return (
            f"{self.fixed_label:<8} {where:<8} "
            f"{self.offset:#08x}..{self.end:#08x}  {self.size // 1024:>4} KiB"
        )


@dataclass(frozen=True)
class UpdateSchemeDef:
    """How one board takes a firmware update, and what that costs in flash.

    ADR 0015 decision 2: this is registry *data*. A new board is a row
    plus a bring-up; a board needing a scheme that does not exist yet is
    what re-opens the ADR.
    """

    #: Board class of ADR 0015, ``"A"`` (external staging flash) or
    #: ``"B"`` (1 MiB internal only). Descriptive — nothing branches on it.
    board_class: str
    #: Key of :data:`MCUBOOT_MODE_SYMBOLS`.
    mcuboot_mode: str
    #: Where the second copy of an image lives: ``"external-flash"`` or
    #: ``"none"``.
    staging: str
    #: How a user reaches the bootloader's recovery path, in the order
    #: they would try them.
    recovery: tuple[str, ...]
    partitions: tuple[PartitionDef, ...]
    #: Smallest erasable unit of every slot, in bytes. MCUboot's swap
    #: needs one sector layout across both slots, so this is one number
    #: per board rather than one per partition — a board whose two parts
    #: disagree needs a different scheme, not a second field.
    erase_block_size: int
    #: Devicetree that puts :attr:`partitions` on the board, verbatim.
    #: Written to *both* images: the bootloader and the application have
    #: to agree on the flash map byte for byte.
    partition_overlay: str
    #: Smallest writable unit of the SoC's **internal** flash, in bytes,
    #: which is what an MCUboot image trailer has to be aligned to and
    #: therefore imgtool's ``--align``. Internal even on a board whose
    #: secondary slot lives on another part: Zephyr's
    #: ``cmake/mcuboot.cmake`` reads the ``zephyr,flash`` chosen node for
    #: it, so this field states what the build does rather than what a
    #: reading of the layout might suggest.
    write_block_size: int = 4
    #: Offset of the MCUboot image header inside a slot, in bytes —
    #: imgtool's ``--header-size``, and the ``CONFIG_ROM_START_OFFSET``
    #: the application is linked with. 0x200 is Zephyr's default for
    #: every Cortex-M target with ``CONFIG_BOOTLOADER_MCUBOOT=y``; a
    #: board that needs another value states it here rather than
    #: anywhere in the builder.
    header_size: int = 0x200
    #: Kconfig for the MCUboot image beyond what sysbuild derives from
    #: the mode, e.g. drivers for an external staging part. Occasionally a
    #: bare ``#`` comment line explaining the group of symbols after it —
    #: this is emitted verbatim, so it reads in the generated file exactly
    #: as it reads here.
    bootloader_kconfig: tuple[str, ...] = ()
    #: Snippets the MCUboot image is built with.
    bootloader_snippets: tuple[str, ...] = ()
    #: Snippets the application image is built with on top of the ones
    #: its own configuration asks for.
    application_snippets: tuple[str, ...] = ()
    #: Kconfig the *application* image needs to receive a Matter OTA on
    #: this scheme, or empty for a scheme that cannot (ADR 0015 decision 5
    #: puts Matter OTA on board class A only — a board with nowhere to
    #: stage a second image has nothing to enable).
    #:
    #: One indivisible group, emitted together or not at all, and emitted
    #: from here rather than expressed as Kconfig ``select``s in
    #: ``components/matter/Kconfig``: MCUHOME_MATTER hangs off Zephyr's
    #: libc choice, and a ``select FLASH``/``STREAM_FLASH``/``IMG_MANAGER``
    #: from under it closes a dependency cycle through REBOOT and
    #: USB_DFU_REBOOT that stops Kconfig parsing the tree at all — in every
    #: build in the repository, not only in one with OTA on. The C side
    #: checks the group by name and says which member is missing.
    #:
    #: Only applied to a device whose configuration actually has Matter
    #: (:func:`mcuhome.resolve._resolve_build`); a class-A board running a
    #: Matter-less device gets none of it.
    matter_ota_kconfig: tuple[str, ...] = ()
    #: Devicetree written *only* to the bootloader image's overlay, after
    #: :attr:`partition_overlay` — never to the application's, which does
    #: not carry the bootloader's dead-weight problems (e.g. a serial
    #: driver MCUboot links unconditionally, ADR 0015 amendment 2026-08-07).
    bootloader_overlay: str = ""
    #: Why :attr:`bootloader_overlay` is there, rendered as its generated
    #: comment (mirrors :attr:`BoardDef.overlay_note`).
    bootloader_overlay_note: str = ""

    @property
    def matter_ota(self) -> bool:
        """Whether a device on this scheme can take a Matter OTA update.

        Derived rather than stated, so that the flag and the Kconfig group
        that implements it cannot disagree (ADR 0015 decision 5).
        """
        return bool(self.matter_ota_kconfig)

    def partition(self, label: str) -> PartitionDef:
        """The partition with this node label, or a KeyError."""
        for entry in self.partitions:
            if entry.label == label:
                return entry
        raise KeyError(label)

    @property
    def imgtool_slot(self) -> PartitionDef:
        """The partition whose size imgtool pads a signed image to.

        Zephyr's ``cmake/mcuboot.cmake`` uses ``slot1_partition`` for
        every mode that has one and ``slot0_partition`` for the
        single-slot modes. The two are equal in size on a swap layout by
        construction, so this rarely changes a number — it changes which
        number the manifest is *stating*, and detached signing
        (:mod:`mcuhome.imgtool`) has to state the one the inline build
        would have used, not one that happens to match today.
        """
        label = (
            "slot1_partition" if self.mcuboot_mode in _SECONDARY_SLOT_MODES else "slot0_partition"
        )
        return self.partition(label)

    @property
    def max_image_sectors(self) -> int:
        """Sectors MCUboot has to be able to track per slot.

        ``CONFIG_BOOT_MAX_IMG_SECTORS_AUTO`` derives this itself — but it
        reads slot0's flash node and applies its block size to slot1,
        which is wrong the moment the two slots live on different parts
        (ADR 0015 decision 3; upstream bug candidate). Every scheme here
        therefore states the number, and this is where it comes from.
        """
        slots = [entry for entry in self.partitions if entry.fixed_label.startswith("image-")]
        return max(entry.size for entry in slots) // self.erase_block_size


# --------------------------------------------------------------------------
# Boards
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class BootstrapDef:
    """How a board reaches the MCUHome standard state (ADR 0016 decision 2).

    A device is bootstrapped **once, ever**: the vendor bootloader is
    replaced by MCUHome's MCUboot, after which every board behaves
    identically — same recovery entrance, same transport, same signing,
    same partition rules. ADR 0016 makes this a per-board ``BoardDef``
    property on purpose ("a new board declares how it is bootstrapped; it
    does not add a code path"), and this is that property.

    :attr:`steps` is the user-facing instruction list the dashboard shows
    on its first rung (dashboard ADR 0010) and the CLI can print. It is
    prose *data*: the builder never parses it, and a board whose
    procedure changes changes a table row.
    """

    #: ``"debug-probe"`` (SWD, which every development kit has),
    #: ``"front-door-replacement"`` (the vendor's own DFU mechanism
    #: accepts a bootloader update — the mechanism of record for boards
    #: without a probe) or ``"coexistence"`` (the vendor bootloader is
    #: kept and MCUboot is installed as its application).
    mechanism: str
    #: The state the board ends up in: ``"standard"`` or
    #: ``"coexistence"``. ADR 0016 supports both; only the first is the
    #: target.
    state: str
    #: Which build artifact the bootstrap writes, by manifest role.
    artifact: str
    #: What the user does, in order, in their own words.
    steps: tuple[str, ...] = ()


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
    #: How this board boots and takes updates (ADR 0015 decision 2).
    #: Every board MCUHome builds for has one; the field is optional only
    #: so that a test fixture can describe a board without repeating a
    #: flash layout it does not exercise.
    update_scheme: UpdateSchemeDef | None = None
    #: How this board reaches the MCUHome standard state (ADR 0016
    #: decision 2). Optional for the same reason as :attr:`update_scheme`.
    bootstrap: BootstrapDef | None = None


#: Board wiring of the nRF5340 application core: it has no RNG peripheral
#: of its own, and its watchdog needs the alias the framework looks for.
#: See :data:`BOARDS`.
_NRF5340_BOARD_NOTE = (
    "Entropy. The nRF5340 application core has no RNG peripheral: the one on the die "
    "belongs to the network core. The board default points zephyr,entropy at the "
    "Bluetooth HCI entropy driver, which cannot work here because MCUHome runs this "
    "SoC Thread-only with Bluetooth off (ADR 0011). Redirect to the framework's IPC "
    "driver, which seeds a CTR-DRBG from the network core's real RNG over a second "
    "endpoint on the ipc0 instance the 802.15.4 spinel channel already uses.\n"
    "The network core must run mcuhome/samples/netcore-radio for this endpoint to "
    "exist; against the upstream 802154_rpmsg image the node comes up and then fails "
    "every entropy request with -EIO rather than falling back to a weaker generator.\n"
    "Watchdog. ADR 0015's health amendment makes a hardware watchdog mandatory for "
    "every application image, and the framework finds it through the watchdog0 alias "
    "(the Zephyr convention). This SoC has the peripheral and this board does not "
    "name it, so the alias is board wiring exactly like the entropy redirection "
    "above. Without it the build stops with a message naming the alias, rather than "
    "producing an image with no watchdog in it."
)

_NRF5340_BOARD_OVERLAY = """\
/ {
\tnetcore_entropy: netcore-entropy {
\t\tcompatible = "mcuhome,entropy-ipc";
\t\tipc = <&ipc0>;
\t\tstatus = "okay";
\t};

\tchosen {
\t\tzephyr,entropy = &netcore_entropy;
\t};

\taliases {
\t\twatchdog0 = &wdt0;
\t};
};"""


#: nRF7002-DK flash layout, ADR 0015 decision 3 (board class A), boot
#: partition size amended 2026-08-07 (see the ADR's amendment section).
#:
#: ``storage_partition`` keeps the address and the size the board's own
#: devicetree gives it (``0xF8000``, 32 KiB), which is what makes an
#: update not a re-commissioning: the Matter fabric credentials and the
#: Thread dataset are in it. ``boot_partition`` is no longer the upstream
#: default as of the amendment — it grew from the 64 KiB upstream ships to
#: 80 KiB — so the overlay below restates it alongside ``slot0_partition``,
#: which shrinks to match, and ``slot1_partition``, which moves to the
#: external part entirely.
_NRF7002DK_PARTITIONS = (
    PartitionDef(label="boot_partition", fixed_label="mcuboot", offset=0x00000, size=80 * 1024),
    PartitionDef(label="slot0_partition", fixed_label="image-0", offset=0x14000, size=912 * 1024),
    PartitionDef(label="storage_partition", fixed_label="storage", offset=0xF8000, size=32 * 1024),
    PartitionDef(
        label="slot1_partition",
        fixed_label="image-1",
        offset=0x00000,
        size=912 * 1024,
        device="mx25r64",
    ),
)

_NRF7002DK_PARTITION_OVERLAY = """\
/delete-node/ &slot1_partition;

&boot_partition {
\treg = <0x00000000 0x00014000>;
};

&slot0_partition {
\treg = <0x00014000 0x000e4000>;
};

&mx25r64 {
\tstatus = "okay";

\tpartitions {
\t\tcompatible = "fixed-partitions";
\t\t#address-cells = <1>;
\t\t#size-cells = <1>;

\t\tslot1_partition: partition@0 {
\t\t\tlabel = "image-1";
\t\t\treg = <0x00000000 0x000e4000>;
\t\t};
\t};
};"""

#: The class-A scheme of ADR 0015 decision 3, as the nRF7002-DK runs it.
#:
#: **The original 64 KiB boot partition was nearly full, and that was
#: measured, not feared.** ADR 0015 decision 3 sized it from a
#: single-slot bootloader with serial recovery and the boot mode
#: (52.2 KiB) and left the swap state machine on top of that as the
#: number this bring-up owed. It was ~12 KiB: the first build of this
#: configuration overflowed the partition by **108 bytes**. What bought
#: the room back was dropping the logging subsystem and the console,
#: which on this board have no backend once serial recovery takes the
#: port — see ``CONFIG_LOG`` below.
#:
#: **Amended 2026-08-07 (ADR 0015 amendment, product owner).** At 64 KiB
#: the bootloader measured 63.1 KiB — 98.6 % full. Two independent size
#: levers were measured against it: link-time optimization, strictly
#: per-image (``CONFIG_LTO`` below; -7.55 KiB), and dropping the UART
#: driver ``MCUBOOT_SERIAL`` links in regardless of the selected serial
#: transport (``bootloader_overlay`` below; -1.30 KiB; upstream
#: imprecision, workspace ``UPSTREAM-BUGS.md`` entry M2). Combined,
#: 55.4 KiB — enough on its own to clear a 15 %-free bar at 64 KiB. The
#: product owner's call was to grow the partition to 80 KiB anyway:
#: future headroom was valued above reclaiming those 16 KiB into
#: ``slot0_partition``, and the decision predates any bootstrapped
#: device, so — unlike the "next feature is a re-bootstrap" framing
#: above — it costs nothing beyond the 16 KiB it takes from slot0.
_CLASS_A_EXTERNAL_STAGING = UpdateSchemeDef(
    board_class="A",
    mcuboot_mode="swap-using-offset",
    staging="external-flash",
    recovery=("serial-recovery", "boot-mode", "button"),
    partitions=_NRF7002DK_PARTITIONS,
    erase_block_size=4096,
    partition_overlay=_NRF7002DK_PARTITION_OVERLAY,
    bootloader_kconfig=(
        # Serial recovery over CDC-ACM: ADR 0016 decision 2 makes it the
        # permanent debugger-free rescue path of every supported board,
        # and ADR 0015 decision 6 makes USB/SMP the one transport that
        # reaches an uncommissioned or half-updated node. The three
        # symbols are MCUboot's own usb_cdc_acm_recovery.conf.
        "CONFIG_MCUBOOT_SERIAL=y",
        "CONFIG_BOOT_SERIAL_CDC_ACM=y",
        "CONFIG_UART_CONSOLE=n",
        # And with the console gone, so is every backend the bootloader's
        # log could reach: this board has no RTT console out of the box,
        # so CONFIG_LOG=y would compile a logging subsystem whose output
        # nothing receives. MCUboot's own CDC-ACM board fragment
        # (boot/zephyr/boards/nrf52840dongle_nrf52840.conf) drops both for
        # exactly this reason. Measured here: without them the bootloader
        # overflows its 64 KiB partition by 108 bytes — see the size note
        # above this scheme.
        "CONFIG_LOG=n",
        "CONFIG_CONSOLE=n",
        # Buttonless entrance (ADR 0016 decision 3): MCUboot reads the
        # retention area the boot-mode snippet puts in GPREGRET1. The
        # physical button entrance stays on — it is what is left when the
        # application no longer boots far enough to write a register.
        "CONFIG_RETAINED_MEM=y",
        "CONFIG_RETENTION=y",
        "CONFIG_RETENTION_BOOT_MODE=y",
        "CONFIG_BOOT_SERIAL_BOOT_MODE=y",
        # The secondary slot is on the MX25R64, which hangs off SPI4 —
        # QSPI carries the nRF7002 Wi-Fi companion — so the driver is
        # SPI_NOR and not NORDIC_QSPI_NOR. MCUboot's own board fragment
        # switches SPI_NOR off for this board, which is right for a
        # bootloader with no external slot and wrong for this one.
        "CONFIG_SPI=y",
        "CONFIG_SPI_NOR=y",
        # Its driver polls, so the bootloader needs a scheduler.
        "CONFIG_MULTITHREADING=y",
        # The layout page size decides the erase unit MCUboot's flash map
        # sees on the external part. Its default is the 64 KiB block
        # erase, which would give the two slots different sector layouts
        # — and swap needs one layout across both.
        "CONFIG_SPI_NOR_FLASH_LAYOUT_PAGE_SIZE=4096",
        # No downgrade prevention in v0.x (ADR 0015 decision 8). Rolling
        # a device back to a known-good image is a normal act during
        # development, and these layouts set no readback protection, so
        # an attacker with the board in hand can erase and re-provision
        # the part outright — this would raise the cost of the
        # developer's operation and not the attacker's. Stated rather
        # than left to the default, because the default is what would
        # change under us. Revisit at 1.0, with readback protection.
        "CONFIG_MCUBOOT_DOWNGRADE_PREVENTION=n",
        # LTO size lever (ADR 0015 amendment, 2026-08-07 — the date stays
        # out of the comment below, which is emitted into the generated
        # mcuboot.conf verbatim like every string in this tuple, and
        # generated output carries no timestamps by construction).
        "",
        "# LTO, this image only (measured -7.55 KiB). Strictly per-image:",
        "# sysbuild gives every image its own Kconfig fragment, so the",
        "# application build is untouched by design.",
        "CONFIG_ISR_TABLES_LOCAL_DECLARATION=y",
        "CONFIG_LTO=y",
        "CONFIG_LTO_SINGLE_THREADED=y",
    ),
    bootloader_snippets=("boot-mode",),
    application_snippets=("boot-mode",),
    # Matter OTA (ADR 0015 decision 5). This scheme is the reason the
    # decision exists: the secondary slot is on the MX25R64, so there is
    # somewhere to stage a downloaded image and somewhere to swap back
    # from. Measured cost on this board: +27.0 KiB flash, +2.0 KiB RAM.
    matter_ota_kconfig=(
        # The framework's own gate. CHIP's requestor plus MCUHome's image
        # processor, which is the pair that makes an external staging slot
        # work at all — CHIP's own processor writes to the internal flash
        # controller and would land inside slot0 and storage.
        "CONFIG_MCUHOME_MATTER_OTA=y",
        "CONFIG_CHIP_OTA_REQUESTOR=y",
        # Zephyr's DFU plumbing underneath it: the flash map to find the
        # staging slot on whichever part it lives, stream_flash to write
        # through a small buffer, img_manager for the MCUboot trailer and
        # boot_request_upgrade(). Measured at +1.3 KiB flash, +0 B RAM.
        "CONFIG_FLASH=y",
        "CONFIG_FLASH_MAP=y",
        "CONFIG_STREAM_FLASH=y",
        # NOT implied by STREAM_FLASH and not defaulted by it: without it
        # stream_flash writes into pages nobody erased, the staged image is
        # silently wrong, and it only shows up after the download, the
        # reboot and the swap attempt.
        "CONFIG_STREAM_FLASH_ERASE=y",
        "CONFIG_IMG_MANAGER=y",
        # Applying an update means rebooting into the swapped image.
        "CONFIG_REBOOT=y",
        # The staging part itself. Same driver as the bootloader side
        # above, and for the same reason: the MX25R64 hangs off SPI4, not
        # QSPI, which the nRF7002 Wi-Fi companion occupies. The layout page
        # size has to match the bootloader's, or the two disagree about
        # where a sector begins.
        "CONFIG_SPI=y",
        "CONFIG_SPI_NOR=y",
        "CONFIG_SPI_NOR_FLASH_LAYOUT_PAGE_SIZE=4096",
    ),
    # Dead UART (measured -1.30 KiB; ADR 0015 amendment, 2026-08-07).
    # MCUBOOT_SERIAL selects SERIAL and UART_INTERRUPT_DRIVEN
    # unconditionally, regardless of which serial-recovery transport is
    # chosen (upstream imprecision, workspace UPSTREAM-BUGS.md entry M2)
    # — with BOOT_SERIAL_CDC_ACM as the transport here, that links in a
    # UART driver the bootloader never opens. Disabling the node it would
    # bind to drops it. Bootloader-only: the application never carries
    # this problem, so it never carries this fix. bootloader_overlay_note
    # is emitted verbatim too, so it carries no date either.
    bootloader_overlay='&uart0 {\n\tstatus = "disabled";\n};',
    # The nRF5340's internal flash writes in 4-byte words (nordic,nrf53-flash-
    # controller, write-block-size = <4>), and Zephyr reads the *internal*
    # controller for imgtool's --align even though slot1 is on the MX25R64.
    write_block_size=4,
    bootloader_overlay_note=(
        "Dead UART. MCUBOOT_SERIAL selects SERIAL and UART_INTERRUPT_DRIVEN "
        "unconditionally, regardless of which serial-recovery transport is chosen "
        "— an upstream imprecision (workspace UPSTREAM-BUGS.md entry M2). With "
        "BOOT_SERIAL_CDC_ACM as the transport here, that links in a UART driver "
        "the bootloader never opens. Disabling the node it would bind to drops "
        "it: measured -1.30 KiB."
    ),
)


#: The nRF7002-DK is a development kit with an on-board J-Link, so its
#: bootstrap is the easy case ADR 0016 decision 2 explicitly does not have
#: to design around: one full-chip flash of the combined hex over SWD
#: writes MCUboot and the application together, and the board is in the
#: standard state afterwards. The front-door replacement mechanism the ADR
#: makes the mechanism of record is for the boards that have no probe.
_NRF7002DK_BOOTSTRAP = BootstrapDef(
    mechanism="debug-probe",
    state="standard",
    artifact="merged",
    steps=(
        "Connect the kit's USB port marked nRF USB to this machine; the on-board "
        "J-Link enumerates as a debug probe.",
        "Flash the combined hex from the build (the merged artifact of the build "
        "manifest) — it carries MCUboot and the signed application at their own "
        "offsets, so one write brings the board into the MCUHome standard state.",
        "After this the board never needs the probe again: updates arrive over "
        "USB through MCUboot's serial recovery, and over the air once the device "
        "is commissioned.",
    ),
)


BOARDS: dict[str, BoardDef] = {
    # nRF7002-DK, application core. Thread-only: BLE is deliberately off
    # (ADR 0011 — vanilla Zephyr has no combined BLE + 802.15.4 netcore
    # image), and the app core has no RNG peripheral, so entropy comes
    # from the network core over IPC.
    "nrf7002dk/nrf5340/cpuapp": BoardDef(
        name="nrf7002dk/nrf5340/cpuapp",
        transports=frozenset({"thread"}),
        kconfig=("CONFIG_BT=n", "CONFIG_ENTROPY_GENERATOR=y"),
        overlay=_NRF5340_BOARD_OVERLAY,
        overlay_note=_NRF5340_BOARD_NOTE,
        update_scheme=_CLASS_A_EXTERNAL_STAGING,
        bootstrap=_NRF7002DK_BOOTSTRAP,
    ),
}

PLANNED_BOARDS: dict[str, str] = {
    "nrf52840dk/nrf52840": "not brought up yet",
    "nrf52840dongle/nrf52840": "brought up as a bare Matter node, but has no I2C bus broken out",
    "nrf54l15dk/nrf54l15/cpuapp": "not brought up yet",
}
