/*
 * SPDX-FileCopyrightText: 2026 The MCUHome Contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * native_sim coverage of the CHIP-free tables-contract validator
 * (components/matter/src/table_validate.c, ADR 0014). CHIP cannot build
 * on native_sim, so this is the only host-run coverage of the validation
 * logic the framework actually links — see the CMakeLists.txt comment for
 * how the source is pulled in without the CHIP-coupled Zephyr module.
 *
 * Every test builds its own small, self-contained tables/limits (never
 * the golden sample) and asserts the exact errno table_validate.c
 * documents, so a change to error codes or validation order shows up here
 * first.
 */

#include <errno.h>

#include <zephyr/kernel.h>
#include <zephyr/sys/util.h>
#include <zephyr/ztest.h>

#include <mcuhome/matter_tables.h>

#include "table_validate.h"

/* --- Shared fixtures -----------------------------------------------------
 *
 * One valid endpoint (one device type, one cluster, one attribute) that
 * every "reject exactly this one thing" test starts from and mutates a
 * single field of. Mirrors the shape of the golden sample
 * (samples/matter-node/src/mcuhome_config.c) without depending on it.
 */

static const struct mcuhome_matter_attr valid_attrs[] = {
	{
		.id = 0x0000,
		.type = MCUHOME_ATTR_TYPE_INT16S,
		.size = 2,
		.flags = 0,
		.store = NULL,
		.def = 0,
	},
};

static const struct mcuhome_matter_cluster valid_clusters[] = {
	{
		.id = 0x0402,
		.feature_map = 0,
		.cluster_revision = 1,
		.attrs = valid_attrs,
		.attr_count = ARRAY_SIZE(valid_attrs),
	},
};

static const struct mcuhome_matter_device_type valid_device_types[] = {
	{
		.id = 0x0302,
		.revision = 1,
	},
};

static const struct mcuhome_matter_endpoint valid_endpoints[] = {
	{
		.endpoint_id = 1,
		.parent_id = 0,
		.device_types = valid_device_types,
		.device_type_count = ARRAY_SIZE(valid_device_types),
		.clusters = valid_clusters,
		.cluster_count = ARRAY_SIZE(valid_clusters),
	},
};

static const struct mcuhome_matter_node valid_node = {
	.tables_version = MCUHOME_MATTER_TABLES_VERSION,
	.endpoints = valid_endpoints,
	.endpoint_count = ARRAY_SIZE(valid_endpoints),
};

/* Matches the Kconfig defaults (components/matter/Kconfig) so the
 * baseline "passes" test reflects a realistic build, not just a
 * deliberately oversized one. Individual tests shrink whichever field
 * they need to trigger a bound cheaply. */
static const struct mcuhome_matter_limits generous_limits = {
	.max_endpoints = 4,
	.max_clusters_per_endpoint = 4,
	.max_attrs_per_endpoint = 16,
	.max_device_types_per_endpoint = 2,
};

/* Builds a single-endpoint, single-cluster node around one attribute and
 * validates it. Used by the global-attribute-range tests, which only vary
 * the attribute ID. */
static int validate_single_attr(uint32_t attr_id)
{
	const struct mcuhome_matter_attr attrs[] = {
		{
			.id = attr_id,
			.type = MCUHOME_ATTR_TYPE_INT32U,
			.size = 4,
			.flags = 0,
			.store = NULL,
			.def = 0,
		},
	};
	const struct mcuhome_matter_cluster clusters[] = {
		{
			.id = 0x0402,
			.feature_map = 0,
			.cluster_revision = 1,
			.attrs = attrs,
			.attr_count = ARRAY_SIZE(attrs),
		},
	};
	struct mcuhome_matter_endpoint endpoint = valid_endpoints[0];
	struct mcuhome_matter_node node = valid_node;

	endpoint.clusters = clusters;
	endpoint.cluster_count = ARRAY_SIZE(clusters);
	node.endpoints = &endpoint;
	node.endpoint_count = 1;

	return mcuhome_matter_validate_node(&node, &generous_limits);
}

/* --- Baseline ------------------------------------------------------------ */

ZTEST(matter_tables, test_valid_single_endpoint_passes)
{
	zassert_equal(mcuhome_matter_validate_node(&valid_node, &generous_limits), 0);
}

ZTEST(matter_tables, test_null_node_rejected)
{
	zassert_equal(mcuhome_matter_validate_node(NULL, &generous_limits), -EINVAL);
}

/* --- Node-level checks ----------------------------------------------------- */

ZTEST(matter_tables, test_tables_version_mismatch_rejected)
{
	struct mcuhome_matter_node node = valid_node;

	node.tables_version = MCUHOME_MATTER_TABLES_VERSION + 1;

	zassert_equal(mcuhome_matter_validate_node(&node, &generous_limits), -EINVAL);
}

ZTEST(matter_tables, test_duplicate_endpoint_ids_rejected)
{
	struct mcuhome_matter_endpoint endpoints[2] = {valid_endpoints[0], valid_endpoints[0]};
	struct mcuhome_matter_node node = valid_node;

	endpoints[0].endpoint_id = 1;
	endpoints[1].endpoint_id = 1;
	node.endpoints = endpoints;
	node.endpoint_count = 2;

	zassert_equal(mcuhome_matter_validate_node(&node, &generous_limits), -EEXIST);
}

/* --- Endpoint-level checks -------------------------------------------------- */

ZTEST(matter_tables, test_endpoint_id_zero_rejected)
{
	struct mcuhome_matter_endpoint endpoint = valid_endpoints[0];
	struct mcuhome_matter_node node = valid_node;

	endpoint.endpoint_id = 0;
	node.endpoints = &endpoint;
	node.endpoint_count = 1;

	/* Collides with the static root endpoint range, not a plain -EINVAL
	 * (table_validate.c, MCUHOME_MATTER_ROOT_ENDPOINT_COUNT). */
	zassert_equal(mcuhome_matter_validate_node(&node, &generous_limits), -EEXIST);
}

ZTEST(matter_tables, test_null_device_types_with_count_rejected)
{
	struct mcuhome_matter_endpoint endpoint = valid_endpoints[0];
	struct mcuhome_matter_node node = valid_node;

	endpoint.device_types = NULL;
	endpoint.device_type_count = 1;
	node.endpoints = &endpoint;
	node.endpoint_count = 1;

	zassert_equal(mcuhome_matter_validate_node(&node, &generous_limits), -EINVAL);
}

ZTEST(matter_tables, test_null_clusters_with_count_rejected)
{
	struct mcuhome_matter_endpoint endpoint = valid_endpoints[0];
	struct mcuhome_matter_node node = valid_node;

	endpoint.clusters = NULL;
	endpoint.cluster_count = 1;
	node.endpoints = &endpoint;
	node.endpoint_count = 1;

	zassert_equal(mcuhome_matter_validate_node(&node, &generous_limits), -EINVAL);
}

ZTEST(matter_tables, test_duplicate_cluster_ids_rejected)
{
	const struct mcuhome_matter_cluster clusters[] = {
		{
			.id = 0x0402,
			.feature_map = 0,
			.cluster_revision = 1,
			.attrs = valid_attrs,
			.attr_count = ARRAY_SIZE(valid_attrs),
		},
		{
			.id = 0x0402,
			.feature_map = 0,
			.cluster_revision = 1,
			.attrs = valid_attrs,
			.attr_count = ARRAY_SIZE(valid_attrs),
		},
	};
	struct mcuhome_matter_endpoint endpoint = valid_endpoints[0];
	struct mcuhome_matter_node node = valid_node;

	endpoint.clusters = clusters;
	endpoint.cluster_count = ARRAY_SIZE(clusters);
	node.endpoints = &endpoint;
	node.endpoint_count = 1;

	zassert_equal(mcuhome_matter_validate_node(&node, &generous_limits), -EEXIST);
}

/* --- Attribute-level checks -------------------------------------------------- */

ZTEST(matter_tables, test_null_attrs_with_count_rejected)
{
	const struct mcuhome_matter_cluster clusters[] = {
		{
			.id = 0x0402,
			.feature_map = 0,
			.cluster_revision = 1,
			.attrs = NULL,
			.attr_count = 1,
		},
	};
	struct mcuhome_matter_endpoint endpoint = valid_endpoints[0];
	struct mcuhome_matter_node node = valid_node;

	endpoint.clusters = clusters;
	endpoint.cluster_count = ARRAY_SIZE(clusters);
	node.endpoints = &endpoint;
	node.endpoint_count = 1;

	zassert_equal(mcuhome_matter_validate_node(&node, &generous_limits), -EINVAL);
}

ZTEST(matter_tables, test_duplicate_attr_ids_rejected)
{
	const struct mcuhome_matter_attr attrs[] = {
		{
			.id = 0x0000,
			.type = MCUHOME_ATTR_TYPE_INT16S,
			.size = 2,
			.flags = 0,
			.store = NULL,
			.def = 0,
		},
		{
			.id = 0x0000,
			.type = MCUHOME_ATTR_TYPE_INT16S,
			.size = 2,
			.flags = 0,
			.store = NULL,
			.def = 0,
		},
	};
	const struct mcuhome_matter_cluster clusters[] = {
		{
			.id = 0x0402,
			.feature_map = 0,
			.cluster_revision = 1,
			.attrs = attrs,
			.attr_count = ARRAY_SIZE(attrs),
		},
	};
	struct mcuhome_matter_endpoint endpoint = valid_endpoints[0];
	struct mcuhome_matter_node node = valid_node;

	endpoint.clusters = clusters;
	endpoint.cluster_count = ARRAY_SIZE(clusters);
	node.endpoints = &endpoint;
	node.endpoint_count = 1;

	zassert_equal(mcuhome_matter_validate_node(&node, &generous_limits), -EEXIST);
}

ZTEST(matter_tables, test_writable_attr_without_store_rejected)
{
	const struct mcuhome_matter_attr attrs[] = {
		{
			.id = 0x0000,
			.type = MCUHOME_ATTR_TYPE_INT16S,
			.size = 2,
			.flags = MCUHOME_ATTR_F_WRITABLE,
			.store = NULL,
			.def = 0,
		},
	};
	const struct mcuhome_matter_cluster clusters[] = {
		{
			.id = 0x0402,
			.feature_map = 0,
			.cluster_revision = 1,
			.attrs = attrs,
			.attr_count = ARRAY_SIZE(attrs),
		},
	};
	struct mcuhome_matter_endpoint endpoint = valid_endpoints[0];
	struct mcuhome_matter_node node = valid_node;

	endpoint.clusters = clusters;
	endpoint.cluster_count = ARRAY_SIZE(clusters);
	node.endpoints = &endpoint;
	node.endpoint_count = 1;

	zassert_equal(mcuhome_matter_validate_node(&node, &generous_limits), -EINVAL);
}

ZTEST(matter_tables, test_global_attr_range_rejected)
{
	zassert_equal(validate_single_attr(0xFFF8), -EINVAL, "GeneratedCommandList");
	zassert_equal(validate_single_attr(0xFFF9), -EINVAL, "AcceptedCommandList");
	zassert_equal(validate_single_attr(0xFFFA), -EINVAL, "EventList (reserved)");
	zassert_equal(validate_single_attr(0xFFFB), -EINVAL, "AttributeList");
	zassert_equal(validate_single_attr(0xFFFC), -EINVAL, "FeatureMap");
	zassert_equal(validate_single_attr(0xFFFD), -EINVAL, "ClusterRevision");
}

ZTEST(matter_tables, test_attr_id_just_below_global_range_allowed)
{
	zassert_equal(validate_single_attr(0xFFF7), 0,
		      "0xFFF7 is one below the reserved range and must be allowed");
}

/* --- Parent-chain checks ----------------------------------------------------- */

ZTEST(matter_tables, test_parent_self_reference_rejected)
{
	struct mcuhome_matter_endpoint endpoint = valid_endpoints[0];
	struct mcuhome_matter_node node = valid_node;

	endpoint.endpoint_id = 1;
	endpoint.parent_id = 1;
	node.endpoints = &endpoint;
	node.endpoint_count = 1;

	zassert_equal(mcuhome_matter_validate_node(&node, &generous_limits), -EINVAL);
}

ZTEST(matter_tables, test_two_endpoint_parent_cycle_rejected)
{
	struct mcuhome_matter_endpoint endpoints[2] = {valid_endpoints[0], valid_endpoints[0]};
	struct mcuhome_matter_node node = valid_node;

	endpoints[0].endpoint_id = 1;
	endpoints[0].parent_id = 2;
	endpoints[1].endpoint_id = 2;
	endpoints[1].parent_id = 1;
	node.endpoints = endpoints;
	node.endpoint_count = 2;

	zassert_equal(mcuhome_matter_validate_node(&node, &generous_limits), -EINVAL);
}

ZTEST(matter_tables, test_parent_undeclared_endpoint_rejected)
{
	struct mcuhome_matter_endpoint endpoint = valid_endpoints[0];
	struct mcuhome_matter_node node = valid_node;

	endpoint.endpoint_id = 1;
	endpoint.parent_id = 99;
	node.endpoints = &endpoint;
	node.endpoint_count = 1;

	zassert_equal(mcuhome_matter_validate_node(&node, &generous_limits), -EINVAL);
}

ZTEST(matter_tables, test_deep_valid_parent_chain_passes)
{
	struct mcuhome_matter_endpoint endpoints[4];
	struct mcuhome_matter_node node = valid_node;

	/* EP1 -> root, EP2 -> EP1, EP3 -> EP2, EP4 -> EP3: a straight chain
	 * one endpoint short of generous_limits.max_endpoints, deep enough
	 * to walk multiple hops without tripping the cycle guard (which
	 * fires past node->endpoint_count steps). */
	for (int i = 0; i < 4; i++) {
		endpoints[i] = valid_endpoints[0];
		endpoints[i].endpoint_id = (uint16_t)(i + 1);
		endpoints[i].parent_id = (uint16_t)i;
	}
	node.endpoints = endpoints;
	node.endpoint_count = 4;

	zassert_equal(mcuhome_matter_validate_node(&node, &generous_limits), 0);
}

/* --- Pool-bound checks ------------------------------------------------------- */

ZTEST(matter_tables, test_endpoint_pool_overflow_rejected)
{
	struct mcuhome_matter_endpoint endpoints[2] = {valid_endpoints[0], valid_endpoints[0]};
	struct mcuhome_matter_node node = valid_node;
	struct mcuhome_matter_limits limits = generous_limits;

	endpoints[0].endpoint_id = 1;
	endpoints[1].endpoint_id = 2;
	node.endpoints = endpoints;
	node.endpoint_count = 2;
	limits.max_endpoints = 1;

	zassert_equal(mcuhome_matter_validate_node(&node, &limits), -ENOSPC);
}

ZTEST(matter_tables, test_cluster_pool_overflow_rejected)
{
	struct mcuhome_matter_limits limits = generous_limits;

	/* valid_node's endpoint declares 1 table cluster, which needs
	 * room for itself plus the framework-appended Descriptor (+1). */
	limits.max_clusters_per_endpoint = 1;

	zassert_equal(mcuhome_matter_validate_node(&valid_node, &limits), -ENOSPC);
}

ZTEST(matter_tables, test_attr_pool_overflow_rejected)
{
	struct mcuhome_matter_limits limits = generous_limits;

	/* valid_node's endpoint needs 4 attribute slots: 1 declared attr +
	 * 2 global (FeatureMap/ClusterRevision) + 1 for the appended
	 * Descriptor's ClusterRevision. */
	limits.max_attrs_per_endpoint = 3;

	zassert_equal(mcuhome_matter_validate_node(&valid_node, &limits), -ENOSPC);
}

ZTEST(matter_tables, test_device_type_pool_overflow_rejected)
{
	struct mcuhome_matter_limits limits = generous_limits;

	limits.max_device_types_per_endpoint = 0;

	zassert_equal(mcuhome_matter_validate_node(&valid_node, &limits), -ENOSPC);
}

ZTEST_SUITE(matter_tables, NULL, NULL, NULL, NULL, NULL);
