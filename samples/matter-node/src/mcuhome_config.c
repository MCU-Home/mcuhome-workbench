/*
 * SPDX-FileCopyrightText: 2026 The MCUHome Contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * GOLDEN FILE — hand-written, but its SHAPE is the contract for builder
 * phase-2 codegen (ADR 0014; builder-pipeline.md §9 golden-file testing).
 * Everything below is what `mcuhome build` will emit from a YAML device
 * configuration: dumb, reviewable, diffable data, one symbol
 * (`mcuhome_node_config`), zero CHIP includes, zero logic.
 *
 * Keep this file in lockstep with <mcuhome/matter_tables.h>: it doubles as
 * the phase-2 codegen regression fixture.
 *
 * Device: one BMP180, exposed as two independent sensor endpoints — the
 * normal MCUHome shape, since Matter has no "combined sensor" device type
 * and a controller expects one endpoint per measured quantity.
 *   EP1, parent 0 (directly under the root node — MCUHome nodes are native
 *   composed nodes, never bridges), device type Temperature Sensor
 *   (0x0302, revision 3), Temperature Measurement cluster (0x0402).
 *   EP2, parent 0, device type Pressure Sensor (0x0305, revision 2),
 *   Pressure Measurement cluster (0x0403).
 *
 * REVISION SOURCING. All revisions below come from CHIP v1.5.1.0's own
 * implementation data model, because that is what CHIP-based nodes report
 * in the field and what the builder will be able to read mechanically:
 * cluster revisions from src/app/zap-templates/zcl/data-model/chip/
 * *-cluster.xml (cross-checked against src/controller/data_model/
 * controller-clusters.matter), device type revisions from
 * .../data-model/chip/matter-devices.xml.
 *
 * The spec scrape shipped alongside it in data_model/1.5.1/ is one
 * revision ahead for every cluster here (TemperatureMeasurement 5 vs 4,
 * PressureMeasurement 4 vs 3 — the extra revision is "Removed P quality"
 * in both cases) and for the Pressure Sensor device type (3 vs 2). Those
 * are the numbers a CHIP release will grow into, not the ones its current
 * code implements. Re-check both on every SDK bump.
 *
 * EP1's Temperature Sensor device type revision was corrected to 3
 * (matter-devices.xml:1849) from the prototype-era hardcoded 1 — the same
 * sourcing rule above, just not applied yet when that endpoint was first
 * written.
 *
 * The Descriptor cluster and the global attributes FeatureMap (0xFFFC) and
 * ClusterRevision (0xFFFD) are deliberately absent: the framework appends
 * and serves them.
 */

#include <zephyr/sys/util.h>

#include <mcuhome/matter_tables.h>

/* Attribute store cells are owned by whoever produces the value — the
 * component/channel instance in a real device, src/main.c in this sample.
 * Generated tables only point at them. */
extern struct mcuhome_attr_store sample_temp_store;
extern struct mcuhome_attr_store sample_press_store;

static const struct mcuhome_matter_attr ep1_temperature_attrs[] = {
	{
		/* MeasuredValue: nullable per the Matter specification — a
		 * sensor that has not produced a reading yet reports null,
		 * not a plausible 0.00 °C. */
		.id = 0x0000,
		.type = MCUHOME_ATTR_TYPE_INT16S,
		.size = 2,
		.flags = MCUHOME_ATTR_F_NULLABLE,
		.store = &sample_temp_store,
		.def = 0,
	},
	{
		/* MinMeasuredValue: constant (store == NULL), -40.00 °C. */
		.id = 0x0001,
		.type = MCUHOME_ATTR_TYPE_INT16S,
		.size = 2,
		.flags = 0,
		.store = NULL,
		.def = -4000,
	},
	{
		/* MaxMeasuredValue: constant (store == NULL), 125.00 °C. */
		.id = 0x0002,
		.type = MCUHOME_ATTR_TYPE_INT16S,
		.size = 2,
		.flags = 0,
		.store = NULL,
		.def = 12500,
	},
};

static const struct mcuhome_matter_cluster ep1_clusters[] = {
	{
		.id = 0x0402, /* Temperature Measurement */
		.feature_map = 0,
		/* ClusterRevision 4 — the revision of TemperatureMeasurement
		 * in the Matter data model shipped with CHIP v1.5.1.0
		 * (src/controller/data_model/controller-clusters.matter).
		 * Re-check on an SDK bump; the builder will read it from the
		 * SDK's .matter files instead of hardcoding it. */
		.cluster_revision = 4,
		.attrs = ep1_temperature_attrs,
		.attr_count = ARRAY_SIZE(ep1_temperature_attrs),
	},
};

static const struct mcuhome_matter_device_type ep1_device_types[] = {
	{
		.id = 0x0302, /* Matter Temperature Sensor */
		.revision = 3,
	},
};

/* --- EP2: pressure ------------------------------------------------------
 *
 * Matter unit for PressureMeasurement::MeasuredValue is 0.1 kPa: the spec
 * defines MeasuredValue = 10 x Pressure with Pressure in kPa (Matter
 * Application Cluster Specification §2.4, Pressure Measurement Cluster;
 * the value is not in CHIP's tree — the XML there is Alchemy-generated
 * and carries types and conformance only, no unit prose). 0.1 kPa is
 * numerically 1 hPa = 1 mbar, so a raw MeasuredValue of 1013 is 1013 hPa,
 * standard sea-level pressure. Nordic's Matter weather station documents
 * exactly that reading for its own BME688-based node.
 */
static const struct mcuhome_matter_attr ep2_pressure_attrs[] = {
	{
		/* MeasuredValue: nullable, same reasoning as EP1 — and the
		 * state the node actually boots into when no sensor is
		 * attached. */
		.id = 0x0000,
		.type = MCUHOME_ATTR_TYPE_INT16S,
		.size = 2,
		.flags = MCUHOME_ATTR_F_NULLABLE,
		.store = &sample_press_store,
		.def = 0,
	},
	{
		/* MinMeasuredValue: constant (store == NULL), 300 hPa.
		 * BMP180 datasheet (BST-BMP180-DS000, §1 "Operating range"):
		 * pressure 300...1100 hPa. The device advertises the sensor's
		 * range, not the cluster's — a controller uses it to size
		 * gauges and to sanity-check readings. */
		.id = 0x0001,
		.type = MCUHOME_ATTR_TYPE_INT16S,
		.size = 2,
		.flags = 0,
		.store = NULL,
		.def = 300,
	},
	{
		/* MaxMeasuredValue: constant (store == NULL), 1100 hPa. */
		.id = 0x0002,
		.type = MCUHOME_ATTR_TYPE_INT16S,
		.size = 2,
		.flags = 0,
		.store = NULL,
		.def = 1100,
	},
};

static const struct mcuhome_matter_cluster ep2_clusters[] = {
	{
		.id = 0x0403, /* Pressure Measurement */
		/* FeatureMap 0: the cluster's only feature is EXT (bit 0,
		 * "Extended range and resolution"), which would make the
		 * ScaledValue/Scale attribute set mandatory. A BMP180's
		 * resolution does not need it. */
		.feature_map = 0,
		/* ClusterRevision 3 — see the revision-sourcing note at the
		 * top of this file. */
		.cluster_revision = 3,
		.attrs = ep2_pressure_attrs,
		.attr_count = ARRAY_SIZE(ep2_pressure_attrs),
	},
};

static const struct mcuhome_matter_device_type ep2_device_types[] = {
	{
		.id = 0x0305, /* Matter Pressure Sensor */
		.revision = 2,
	},
};

static const struct mcuhome_matter_endpoint endpoints[] = {
	{
		.endpoint_id = 1,
		.parent_id = 0, /* directly under the root node */
		.device_types = ep1_device_types,
		.device_type_count = ARRAY_SIZE(ep1_device_types),
		.clusters = ep1_clusters,
		.cluster_count = ARRAY_SIZE(ep1_clusters),
	},
	{
		.endpoint_id = 2,
		.parent_id = 0, /* a sibling of EP1, not a child of it */
		.device_types = ep2_device_types,
		.device_type_count = ARRAY_SIZE(ep2_device_types),
		.clusters = ep2_clusters,
		.cluster_count = ARRAY_SIZE(ep2_clusters),
	},
};

const struct mcuhome_matter_node mcuhome_node_config = {
	.tables_version = MCUHOME_MATTER_TABLES_VERSION,
	.endpoints = endpoints,
	.endpoint_count = ARRAY_SIZE(endpoints),
};
