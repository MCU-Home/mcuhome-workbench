# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Pipeline stage 3: defaults, completion, numbering — the canonical model.

Stage 3 runs on a configuration that stage 2 already accepted, so it can
look things up in :mod:`mcuhome.registry` without re-checking that they
exist. Its job is to turn "what the user wrote" into "what the generators
need", and to make every derived value explicit exactly once:

* **defaults** — power source, Matter on/off, Thread role, sampling
  period, measurement range;
* **device-type completion** — the Matter device type's ID and revision,
  and the full attribute set of every cluster it mandates, including the
  constant Min/MaxMeasuredValue pair that carries the sensor's range;
* **endpoint numbering** — endpoint IDs (1-based; the root endpoint 0 is
  the framework's, ADR 0014) and the RAM cell names the generated tables
  and the channel table will share;
* **unit conversion** — the YAML speaks °C and hPa, the Matter attributes
  are 0.01 °C and 0.1 kPa, and the Zephyr sensor API speaks a third
  system again. All three meet here, as integers, never at runtime;
* **commissioning credentials** — read out of the configuration, never
  invented (:func:`_resolve_pairing`), with the PBKDF2 iteration count as
  the one builder-owned constant among them.
"""

from __future__ import annotations

from fractions import Fraction

from mcuhome import ota, pairing, registry
from mcuhome.errors import ConfigError, ErrorCollector
from mcuhome.model import (
    AttrModel,
    BuildModel,
    BusModel,
    ChannelModel,
    ClusterModel,
    DeviceMeta,
    DeviceModel,
    DeviceTypeModel,
    EndpointModel,
    HardwareModel,
    NetworkModel,
    PairingModel,
    PeripheralModel,
    SourceModel,
    ThreadModel,
    ToolchainModel,
)
from mcuhome.schema import RawConfig, RawEndpoint, assign_endpoint_ids
from mcuhome.toolchain import resolve_toolchain

__all__ = [
    "DEFAULT_SAMPLING_BATTERY_MS",
    "DEFAULT_SAMPLING_MAINS_MS",
    "resolve",
    "store_id",
]

#: Sampling defaults when a cluster does not say (yaml-schema.md §3:
#: "power.source: battery switches defaults conservatively"). Every wake
#: costs radio time on a battery node, so it samples five times slower.
DEFAULT_SAMPLING_MAINS_MS = 60_000
DEFAULT_SAMPLING_BATTERY_MS = 300_000

#: Snippet the Matter framework refuses to build without
#: (components/matter/CMakeLists.txt).
MATTER_SNIPPET = "matter"


def store_id(endpoint_id: int, cluster_name: str, attr_name: str) -> str:
    """Name of the RAM cell behind one attribute.

    The Matter tables and the channel table must point at the *same*
    cell (``<mcuhome/channel.h>``), so the name is derived from the
    attribute's own coordinates and from nothing else.
    """
    return f"ep{endpoint_id}_{cluster_name}_{attr_name}"


def resolve(config: RawConfig) -> DeviceModel:
    """Stage 3. Expects a configuration that passed :func:`~mcuhome.validate.validate`."""
    device = config.device
    board = registry.BOARDS[device.board or ""]
    power_source = (device.power.source if device.power else None) or "mains"

    version = device.version or ota.DEFAULT_VERSION

    network = _resolve_network(config)
    toolchain = _resolve_toolchain(config)
    hardware = _resolve_hardware(config)
    endpoints, channels = _resolve_node(config, power_source)
    build = _resolve_build(board, network, endpoints, channels, version)

    return DeviceModel(
        device=DeviceMeta(
            name=device.name or "",
            friendly_name=device.friendly_name or (device.name or ""),
            board=board.name,
            power_source=power_source,
            version=version,
            source=config.file.name,
        ),
        network=network,
        toolchain=toolchain,
        hardware=hardware,
        endpoints=endpoints,
        channels=channels,
        build=build,
    )


# --------------------------------------------------------------------------
# network / toolchain
# --------------------------------------------------------------------------


def _resolve_network(config: RawConfig) -> NetworkModel:
    raw = config.network
    if raw is None:
        return NetworkModel(transport=None, matter_enabled=False)

    power_source = (config.device.power.source if config.device.power else None) or "mains"
    thread: ThreadModel | None = None
    transport: str | None = None
    if raw.thread is not None:
        transport = "thread"
        # A battery device that has to keep its receiver on is a
        # contradiction, so the conservative default for battery power is
        # the end-device role, not the router role.
        default_role = "mtd" if power_source == "battery" else "ftd"
        thread = ThreadModel(
            device_role=raw.thread.device_role or default_role,
            poll_interval_ms=raw.thread.poll_interval_ms,
        )
    elif raw.wifi is not None:  # pragma: no cover - rejected in stage 2
        transport = "wifi"

    matter_enabled = transport is not None
    if raw.matter is not None and raw.matter.enabled is not None:
        matter_enabled = raw.matter.enabled

    return NetworkModel(
        transport=transport,
        matter_enabled=matter_enabled,
        thread=thread,
        pairing=_resolve_pairing(config, matter_enabled),
    )


def _resolve_pairing(config: RawConfig, matter_enabled: bool) -> PairingModel | None:
    """The commissioning credentials, exactly as the configuration states them.

    Nothing is invented here — that is the whole design (yaml-schema.md
    §4.1). The credentials are random, but they are randomized *once*, by
    ``mcuhome init-pairing``, into the configuration file; from there on
    they are ordinary input and the builder stays byte-deterministic. The
    iteration count is the one derived value, and it is a constant of the
    builder rather than of the device.
    """
    matter = config.network.matter if config.network is not None else None
    if not matter_enabled:
        return None
    if matter is not None and matter.use_test_pairing:
        return PairingModel(
            discriminator=pairing.TEST_DISCRIMINATOR,
            passcode=pairing.TEST_PASSCODE,
            salt=pairing.TEST_SALT,
            iterations=pairing.TEST_ITERATIONS,
            test_credentials=True,
        )
    if (
        matter is None
        or matter.discriminator is None
        or matter.passcode is None
        or matter.salt is None
    ):
        # Unreachable through the CLI: mcuhome.validate refuses this
        # configuration. Reachable by a caller that skipped stage 2, and
        # the quiet alternatives are both wrong — omitting the symbols
        # would silently ship CHIP's published test passcode, and making
        # one up would ship a passcode nobody wrote down.
        raise ConfigError(
            "This configuration reached the code generator without commissioning credentials.",
            location=matter.loc if matter is not None else config.loc,
            hint=(
                "run mcuhome validate on it first; the credentials come from\n"
                f"    mcuhome init-pairing {config.device.name or '<device>'}"
            ),
        )
    return PairingModel(
        discriminator=matter.discriminator,
        passcode=matter.passcode,
        salt=matter.salt,
        iterations=pairing.DEFAULT_ITERATIONS,
        test_credentials=False,
    )


def _resolve_toolchain(config: RawConfig) -> ToolchainModel:
    device = config.device
    resolved = resolve_toolchain(
        board=device.board,
        blob_usage=device.blob_usage,
        zephyr_version=device.zephyr_version,
        blobs=device.blobs,
        blob_locs=device.blob_locs,
        version_loc=device.loc_of("zephyr_version"),
        errors=ErrorCollector(),
    )
    return ToolchainModel(
        zephyr_line=resolved.zephyr_line,
        blob_usage=resolved.blob_usage,
        blobs=resolved.blobs,
    )


# --------------------------------------------------------------------------
# hardware
# --------------------------------------------------------------------------


def _resolve_hardware(config: RawConfig) -> HardwareModel:
    raw = config.hardware
    if raw is None:
        return HardwareModel()

    kinds = {
        peripheral.bus: registry.DRIVERS[peripheral.driver or ""].bus
        for peripheral in raw.peripherals.values()
        if peripheral.bus is not None and peripheral.driver in registry.DRIVERS
    }

    buses = [
        BusModel(
            id=bus.id,
            kind=kinds.get(bus.id) or "i2c",
            controller=bus.controller,
            sda=bus.sda,
            scl=bus.scl,
            frequency_hz=bus.frequency_hz,
        )
        for bus in raw.buses.values()
    ]

    peripherals = []
    for peripheral in raw.peripherals.values():
        driver = registry.DRIVERS[peripheral.driver or ""]
        peripherals.append(
            PeripheralModel(
                id=peripheral.id,
                compatible=driver.compatible,
                bus=peripheral.bus,
                reg=peripheral.address if peripheral.address is not None else driver.fixed_address,
                properties=dict(peripheral.props),
            )
        )

    return HardwareModel(buses=buses, peripherals=peripherals)


# --------------------------------------------------------------------------
# node
# --------------------------------------------------------------------------


def _resolve_node(
    config: RawConfig, power_source: str
) -> tuple[list[EndpointModel], list[ChannelModel]]:
    if config.node is None:
        return [], []

    ids = assign_endpoint_ids(config.node)
    endpoints: list[EndpointModel] = []
    channels: list[ChannelModel] = []

    for endpoint in config.node.endpoints:
        endpoint_id = ids[endpoint.index]
        model, endpoint_channels = _resolve_endpoint(config, endpoint, endpoint_id, power_source)
        endpoints.append(model)
        channels.extend(endpoint_channels)

    return endpoints, channels


def _resolve_endpoint(
    config: RawConfig,
    endpoint: RawEndpoint,
    endpoint_id: int,
    power_source: str,
) -> tuple[EndpointModel, list[ChannelModel]]:
    device_type = registry.DEVICE_TYPES[endpoint.device_type or ""]
    clusters: list[ClusterModel] = []
    channels: list[ChannelModel] = []

    for cluster in endpoint.clusters.values():
        definition = registry.CLUSTERS[cluster.name]
        low, high = (
            (cluster.range.min, cluster.range.max)
            if cluster.range is not None
            else definition.default_range
        )

        attrs: list[AttrModel] = []
        measured_store = ""
        for attr in definition.attrs:
            flags = [
                name
                for name, present in (("writable", attr.writable), ("nullable", attr.nullable))
                if present
            ]
            if attr.role == "measured_value":
                measured_store = store_id(endpoint_id, cluster.name, attr.name)
                attrs.append(
                    AttrModel(
                        id=attr.id,
                        name=attr.name,
                        type=attr.type,
                        size=registry.ATTR_SIZES[attr.type],
                        flags=flags,
                        store=measured_store,
                        default=0,
                    )
                )
                continue
            bound = low if attr.role == "min_measured_value" else high
            attrs.append(
                AttrModel(
                    id=attr.id,
                    name=attr.name,
                    type=attr.type,
                    size=registry.ATTR_SIZES[attr.type],
                    flags=flags,
                    store=None,
                    default=_to_raw(bound, definition),
                )
            )

        clusters.append(
            ClusterModel(
                id=definition.id,
                name=definition.name,
                feature_map=definition.feature_map,
                cluster_revision=definition.revision,
                attrs=attrs,
            )
        )

        if cluster.source is not None:
            channels.append(
                _resolve_channel(
                    config,
                    definition=definition,
                    endpoint_id=endpoint_id,
                    store=measured_store,
                    source=cluster.source,
                    sampling_ms=cluster.sampling_ms or _default_sampling(power_source),
                    delta=cluster.report.delta if cluster.report else None,
                )
            )

    model = EndpointModel(
        id=endpoint_id,
        # MCUHome nodes are native composed nodes, never bridges: every
        # application endpoint sits directly under the root (ADR 0014).
        parent_id=0,
        alias=endpoint.alias,
        device_types=[
            DeviceTypeModel(
                id=device_type.id,
                revision=device_type.revision,
                name=device_type.name,
            )
        ],
        clusters=clusters,
    )
    return model, channels


def _default_sampling(power_source: str) -> int:
    return DEFAULT_SAMPLING_BATTERY_MS if power_source == "battery" else DEFAULT_SAMPLING_MAINS_MS


def _to_raw(value: float, definition: registry.ClusterDef) -> int:
    """Convert a YAML value (°C, hPa, …) into the attribute's raw unit."""
    return int(round(Fraction(value).limit_denominator(1_000_000) * definition.raw_per_unit))


def _resolve_channel(
    config: RawConfig,
    *,
    definition: registry.ClusterDef,
    endpoint_id: int,
    store: str,
    source: str,
    sampling_ms: int,
    delta: float | None,
) -> ChannelModel:
    peripheral_id, channel_name = source.split(".", 1)
    peripheral = config.hardware.peripherals[peripheral_id]  # type: ignore[union-attr]
    driver = registry.DRIVERS[peripheral.driver or ""]
    driver_channel = driver.channels[channel_name]
    scale_num, scale_den = definition.zephyr_scale

    return ChannelModel(
        store=store,
        type=definition.measured_attr.type,
        endpoint_id=endpoint_id,
        cluster_id=definition.id,
        attr_id=definition.measured_attr.id,
        sample_period_ms=sampling_ms,
        # 0 means "publish every sample" (<mcuhome/channel.h>), which is
        # the honest default when the config does not ask for less noise.
        report_delta=_to_raw(delta, definition) if delta is not None else 0,
        source=SourceModel(
            peripheral=peripheral_id,
            channel=f"{peripheral_id}.{channel_name}",
            fetch_channel=driver_channel.fetch_channel,
            zephyr_channel=driver_channel.zephyr_channel,
            scale_num=scale_num,
            scale_den=scale_den,
            offset=0,
        ),
    )


# --------------------------------------------------------------------------
# build inputs
# --------------------------------------------------------------------------


def _resolve_build(
    board: registry.BoardDef,
    network: NetworkModel,
    endpoints: list[EndpointModel],
    channels: list[ChannelModel],
    version: str,
) -> BuildModel:
    """Kconfig fragments and snippets, derived from the resolved model.

    Grounded in the hardware-verified configuration of
    ``samples/matter-node`` — every symbol below is one the running node
    sets. Chip drivers deliberately do not appear: a Zephyr sensor driver
    defaults to ``y`` from its devicetree node ("hardware in DTS, never
    in Kconfig").
    """
    kconfig = ["CONFIG_MCUHOME=y"]
    snippets: list[str] = []

    if network.matter_enabled:
        snippets.append(MATTER_SNIPPET)
        kconfig += [
            "CONFIG_MCUHOME_MATTER=y",
            "CONFIG_CHIP=y",
            "CONFIG_STD_CPP17=y",
            # The commissioning identity — vendor/product IDs,
            # discriminator, passcode, salt, iterations and the verifier —
            # is deliberately absent from this list. It is one indivisible
            # group emitted by mcuhome.pairing.kconfig_lines() straight
            # into the fragment (mcuhome/generate.py), so that no code
            # path can produce a passcode without the verifier derived
            # from it.
            "CONFIG_CHIP_ENABLE_PAIRING_AUTOSTART=y",
            "CONFIG_MBEDTLS=y",
            "CONFIG_MBEDTLS_PSA_CRYPTO_C=y",
        ]
        # CONFIG_CHIP_PROJECT_CONFIG deliberately does NOT appear here: it
        # names a file inside the generated application tree, which is a
        # fact of stage 4's layout and not of the device. The prj.conf
        # emitter appends it (mcuhome/generate.py).
        if endpoints:
            # ADR 0014: the CHIP-side dynamic endpoint count derives from
            # this symbol, so the builder only has to state the number of
            # endpoints it actually generated.
            kconfig.append(f"CONFIG_MCUHOME_MATTER_MAX_DYNAMIC_ENDPOINTS={len(endpoints)}")

    if network.transport == "thread":
        kconfig.append("CONFIG_NET_L2_OPENTHREAD=y")
        if network.thread is not None and network.matter_enabled:
            role_symbol = {
                "ftd": "CONFIG_MCUHOME_MATTER_THREAD_ROUTER=y",
                "mtd": "CONFIG_MCUHOME_MATTER_THREAD_MED=y",
            }[network.thread.device_role]
            kconfig.append(role_symbol)

    if channels:
        kconfig += ["CONFIG_SENSOR=y", "CONFIG_MCUHOME_SENSOR=y"]

    kconfig += list(board.kconfig)
    # MCUboot's image version and, on a Matter device, the SoftwareVersion
    # a controller compares against the image an OTA provider offers. One
    # group from one string (ADR 0015 decision 9, mcuhome.ota): a build in
    # which those two disagree updates to an image the controller then
    # reports as the wrong version, and nothing warns.
    kconfig += ota.kconfig_lines(version, matter=network.matter_enabled)
    if board.update_scheme is not None:
        # Snippets the board's update scheme needs in the *application*
        # image (ADR 0015 decision 2). They come last so a device's own
        # snippets stay in front: Zephyr lets a later fragment override an
        # earlier one, and board wiring is the more specific statement.
        snippets += [
            snippet
            for snippet in board.update_scheme.application_snippets
            if snippet not in snippets
        ]
        # Matter OTA is board-class data, never a global constant and
        # never a device's own choice (ADR 0015 decision 5): a board with
        # a staging slot can receive one, a board without cannot, and a
        # device with no Matter stack has nothing to receive it with.
        if network.matter_enabled:
            kconfig += list(board.update_scheme.matter_ota_kconfig)
    return BuildModel(snippets=snippets, kconfig=sorted(set(kconfig)))
