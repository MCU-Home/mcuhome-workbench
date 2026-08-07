# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Golden-file test of the canonical model (builder-pipeline.md §9).

The fixture is the resolved model of ``00-bmp180-two-endpoints.yaml``,
the YAML form of ``samples/matter-node``. Its numbers are cross-checked
against the hand-written golden tables in
``samples/matter-node/src/mcuhome_config.c`` and ``src/main.c``: those
files are what the code generator has to reproduce, so if this model ever
stops matching them, the generator would emit a different device than the
one that was commissioned into a real Home Assistant.
"""

from __future__ import annotations

from conftest import EXAMPLES_DIR, FIXTURE_TREE, GOLDEN_DIR, resolve_file

from mcuhome.model import MODEL_VERSION, DeviceModel

GOLDEN = GOLDEN_DIR / "00-bmp180-two-endpoints.device-model.json"


def test_model_matches_the_golden_file() -> None:
    model = resolve_file(EXAMPLES_DIR / "00-bmp180-two-endpoints.yaml")
    assert model.to_json() == GOLDEN.read_text(encoding="utf-8")


def test_model_round_trips_through_json() -> None:
    model = resolve_file(EXAMPLES_DIR / "00-bmp180-two-endpoints.yaml")
    assert DeviceModel.from_json(model.to_json()) == model
    assert model.model_version == MODEL_VERSION


def test_model_matches_the_hand_written_matter_tables() -> None:
    """Cross-check against samples/matter-node/src/mcuhome_config.c."""
    model = resolve_file(EXAMPLES_DIR / "00-bmp180-two-endpoints.yaml")
    endpoint1, endpoint2 = model.endpoints

    assert (endpoint1.id, endpoint1.parent_id) == (1, 0)
    assert [(item.id, item.revision) for item in endpoint1.device_types] == [(0x0302, 3)]
    cluster1 = endpoint1.clusters[0]
    assert (cluster1.id, cluster1.cluster_revision, cluster1.feature_map) == (0x0402, 4, 0)
    assert [(attr.id, attr.type, attr.size, attr.default) for attr in cluster1.attrs] == [
        (0x0000, "int16s", 2, 0),
        (0x0001, "int16s", 2, -4000),
        (0x0002, "int16s", 2, 8500),
    ]
    assert cluster1.attrs[0].flags == ["nullable"]
    assert cluster1.attrs[0].store is not None
    assert cluster1.attrs[1].store is None  # constant attribute

    assert (endpoint2.id, endpoint2.parent_id) == (2, 0)
    assert [(item.id, item.revision) for item in endpoint2.device_types] == [(0x0305, 2)]
    cluster2 = endpoint2.clusters[0]
    assert (cluster2.id, cluster2.cluster_revision) == (0x0403, 3)
    assert [attr.default for attr in cluster2.attrs] == [0, 300, 1100]


def test_model_matches_the_hand_written_channel_table() -> None:
    """Cross-check against samples/matter-node/src/main.c."""
    model = resolve_file(EXAMPLES_DIR / "00-bmp180-two-endpoints.yaml")
    temperature, pressure = model.channels

    assert temperature.endpoint_id == 1
    assert (temperature.cluster_id, temperature.attr_id) == (0x0402, 0x0000)
    assert temperature.sample_period_ms == 10_000
    assert temperature.report_delta == 10  # 0.10 °C in units of 0.01 °C
    assert temperature.source.zephyr_channel == "SENSOR_CHAN_DIE_TEMP"
    assert temperature.source.fetch_channel == "SENSOR_CHAN_ALL"
    assert (temperature.source.scale_num, temperature.source.scale_den) == (100, 1)

    assert pressure.endpoint_id == 2
    assert (pressure.cluster_id, pressure.attr_id) == (0x0403, 0x0000)
    assert pressure.report_delta == 1  # 1 hPa in units of 0.1 kPa
    assert pressure.source.zephyr_channel == "SENSOR_CHAN_PRESS"
    assert (pressure.source.scale_num, pressure.source.scale_den) == (10, 1)

    # Both channels publish into the cells the Matter tables point at.
    stores = {
        attr.store
        for endpoint in model.endpoints
        for c in endpoint.clusters
        for attr in c.attrs
        if attr.store
    }
    assert {channel.store for channel in model.channels} == stores


def test_defaults_are_applied() -> None:
    model = resolve_file(FIXTURE_TREE / "devices" / "bench-node" / "main.yaml")

    assert model.device.friendly_name == "Bench Node"  # from secrets.yaml
    assert model.device.power_source == "battery"
    # Battery power picks the conservative Thread role and a slow sample.
    assert model.network.thread is not None
    assert model.network.thread.device_role == "mtd"
    assert model.channels[0].sample_period_ms == 300_000
    # No report delta given: every sample is published.
    assert model.channels[0].report_delta == 0
    # Endpoint numbering without explicit ids starts at 1.
    assert [endpoint.id for endpoint in model.endpoints] == [1]
    # The I2C address comes from the driver: a BMP180 has no address pin.
    assert model.hardware.peripherals[0].reg == 0x77
    # The measurement range falls back to the cluster default.
    assert [attr.default for attr in model.endpoints[0].clusters[0].attrs] == [0, -4000, 12500]
    # A Minimal End Device selects the MED Kconfig choice, not the router.
    assert "CONFIG_MCUHOME_MATTER_THREAD_MED=y" in model.build.kconfig
