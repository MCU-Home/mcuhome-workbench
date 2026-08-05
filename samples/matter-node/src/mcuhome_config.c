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
 * Device: a single temperature sensor.
 *   EP1, parent 0 (directly under the root node — MCUHome nodes are native
 *   composed nodes, never bridges), device type Temperature Sensor
 *   (0x0302, revision 1), Temperature Measurement cluster (0x0402).
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
		.revision = 1,
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
};

const struct mcuhome_matter_node mcuhome_node_config = {
	.tables_version = MCUHOME_MATTER_TABLES_VERSION,
	.endpoints = endpoints,
	.endpoint_count = ARRAY_SIZE(endpoints),
};
