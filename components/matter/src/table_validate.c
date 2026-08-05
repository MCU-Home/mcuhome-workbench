/*
 * SPDX-FileCopyrightText: 2026 The MCUHome Contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * Pure-C validation of the generated-tables contract (ADR 0014,
 * <mcuhome/matter_tables.h>). Extracted out of endpoint_registry.cpp so it
 * builds and is unit-testable on native_sim, where CHIP cannot compile
 * (tests/matter_tables/): this file includes no CHIP/ember header and no
 * Zephyr kernel API, only <mcuhome/matter_tables.h> and Zephyr logging.
 *
 * A handful of constants below mirror CHIP/ember facts that
 * endpoint_registry.cpp knows from the real headers (the reserved
 * endpoint ID, the Descriptor cluster ID, the static root endpoint
 * count). They are re-stated here as plain values instead of shared
 * definitions precisely because this file must not include what defines
 * them; each is commented with the CHIP symbol it mirrors so a CHIP
 * version bump can re-verify them at a glance.
 */

#include <errno.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include <zephyr/logging/log.h>

#include <mcuhome/matter_tables.h>

#include "table_validate.h"

LOG_MODULE_REGISTER(mcuhome_matter_tables, CONFIG_LOG_DEFAULT_LEVEL);

/* Endpoint IDs below this count collide with the statically compiled
 * endpoint range. Mirrors CHIP's FIXED_ENDPOINT_COUNT
 * (zap-generated/endpoint_config.h), which a static_assert in
 * matter_init.cpp pins to 1: the framework ZAP defines the root endpoint
 * (EP0) and nothing else (ADR 0014, decision B). Restated as a literal
 * here so this file does not need the generated header to know it. */
#define MCUHOME_MATTER_ROOT_ENDPOINT_COUNT 1u

/* Mirrors chip::kInvalidEndpointId (src/lib/core/DataModelTypes.h). */
#define MCUHOME_MATTER_INVALID_ENDPOINT_ID 0xFFFFu

/* Mirrors chip::app::Clusters::Descriptor::Id (Matter cluster code 0x001D,
 * zzz_generated/app-common/clusters/Descriptor/Ids.h). A table declaring
 * this cluster would collide with the one the framework appends. */
#define MCUHOME_MATTER_DESCRIPTOR_CLUSTER_ID 0x001Du

/* Full code-generated global attribute range (Matter core spec, "Global
 * Elements"): GeneratedCommandList (0xFFF8), AcceptedCommandList (0xFFF9),
 * EventList (0xFFFA, reserved), AttributeList (0xFFFB), FeatureMap
 * (0xFFFC) and ClusterRevision (0xFFFD). CHIP's CodegenDataModelProvider
 * appends GlobalAttributesNotInMetadata {0xFFF8, 0xFFF9, 0xFFFB} to every
 * cluster's AttributeList itself, and the framework separately serves
 * FeatureMap / ClusterRevision — a table declaring any ID in this range
 * would duplicate an AttributeList entry on the wire. Reject the whole
 * range, not just the two IDs the framework happens to serve. */
#define MCUHOME_MATTER_GLOBAL_ATTR_RANGE_START 0xFFF8u
#define MCUHOME_MATTER_GLOBAL_ATTR_RANGE_END   0xFFFDu /* inclusive */

/* Every table cluster costs its own attributes plus FeatureMap and
 * ClusterRevision. */
#define MCUHOME_MATTER_GLOBAL_ATTRS_PER_CLUSTER 2u

/*
 * The one and only place in this file where the contract's type enum
 * meets a wire size. This is NOT the CHIP/ember type mapping — that stays
 * solely in endpoint_registry.cpp's MapType(), which is the one place
 * that may know ZAP_TYPE()/EmberAfAttributeType (ADR 0014) — it is the
 * independent, CHIP-free half of the same fact. Adding a
 * mcuhome_attr_type enumerator means extending both switches and bumping
 * MCUHOME_MATTER_TABLES_VERSION (see <mcuhome/matter_tables.h>).
 */
static bool attr_type_size(enum mcuhome_attr_type type, uint16_t *size)
{
	switch (type) {
	case MCUHOME_ATTR_TYPE_INT16S:
	case MCUHOME_ATTR_TYPE_INT16U:
		*size = 2;
		return true;
	case MCUHOME_ATTR_TYPE_INT32S:
	case MCUHOME_ATTR_TYPE_INT32U:
		*size = 4;
		return true;
	case MCUHOME_ATTR_TYPE_BOOL:
		*size = 1;
		return true;
	}
	return false;
}

static int validate_attr(const struct mcuhome_matter_attr *attr)
{
	uint16_t type_size;

	if (!attr_type_size(attr->type, &type_size)) {
		LOG_ERR("attr 0x%08x: unknown type %d (contract v%u)", attr->id, (int)attr->type,
			MCUHOME_MATTER_TABLES_VERSION);
		return -EINVAL;
	}
	if (attr->size != type_size) {
		LOG_ERR("attr 0x%08x: size %u does not match its type (expected %u)", attr->id,
			attr->size, type_size);
		return -EINVAL;
	}
	if (attr->id >= MCUHOME_MATTER_GLOBAL_ATTR_RANGE_START &&
	    attr->id <= MCUHOME_MATTER_GLOBAL_ATTR_RANGE_END) {
		LOG_ERR("attr 0x%08x: code-generated global attributes (0xFFF8-0xFFFD) are "
			"framework-owned",
			attr->id);
		return -EINVAL;
	}
	if ((attr->flags & MCUHOME_ATTR_F_WRITABLE) != 0 && attr->store == NULL) {
		LOG_ERR("attr 0x%08x: writable attributes need a store cell", attr->id);
		return -EINVAL;
	}
	return 0;
}

static int validate_endpoint(const struct mcuhome_matter_endpoint *endpoint,
			     const struct mcuhome_matter_limits *limits)
{
	size_t attr_slots = MCUHOME_MATTER_DESCRIPTOR_ATTR_COUNT;

	/* Defense in depth. emberAfSetDynamicEndpoint() checks uniqueness
	 * only against other *dynamic* endpoints — its loop starts at
	 * FIXED_ENDPOINT_COUNT (attribute-storage.cpp), so a dynamic
	 * endpoint that shadows a statically compiled one is accepted and
	 * silently produces two endpoints with the same ID. Upstream-fix
	 * candidate; until then we refuse the collision ourselves. */
	if (endpoint->endpoint_id < MCUHOME_MATTER_ROOT_ENDPOINT_COUNT) {
		LOG_ERR("endpoint %u collides with the static endpoint range [0, %u)",
			endpoint->endpoint_id, (unsigned int)MCUHOME_MATTER_ROOT_ENDPOINT_COUNT);
		return -EEXIST;
	}
	if (endpoint->endpoint_id == MCUHOME_MATTER_INVALID_ENDPOINT_ID) {
		LOG_ERR("endpoint id 0x%04x is reserved", endpoint->endpoint_id);
		return -EINVAL;
	}
	if (endpoint->device_type_count == 0) {
		LOG_ERR("endpoint %u has no device type", endpoint->endpoint_id);
		return -EINVAL;
	}
	if (endpoint->device_type_count > limits->max_device_types_per_endpoint) {
		LOG_ERR("endpoint %u: %u device types exceed "
			"CONFIG_MCUHOME_MATTER_MAX_DEVICE_TYPES_PER_ENDPOINT (%u)",
			endpoint->endpoint_id, (unsigned int)endpoint->device_type_count,
			(unsigned int)limits->max_device_types_per_endpoint);
		return -ENOSPC;
	}
	/* NULL-with-nonzero-count checks below run BEFORE anything
	 * dereferences the pointer they guard: a malformed table must be
	 * refused here, not turn into a NULL dereference in this function's
	 * own loops or later in endpoint_registry.cpp's translation. */
	if (endpoint->device_types == NULL && endpoint->device_type_count > 0) {
		LOG_ERR("endpoint %u: device_type_count %u but device_types is NULL",
			endpoint->endpoint_id, (unsigned int)endpoint->device_type_count);
		return -EINVAL;
	}
	/* +1: the Descriptor cluster the framework appends below. */
	if (endpoint->cluster_count + 1 > limits->max_clusters_per_endpoint) {
		LOG_ERR("endpoint %u: %u clusters (+1 Descriptor) exceed "
			"CONFIG_MCUHOME_MATTER_MAX_CLUSTERS_PER_ENDPOINT (%u)",
			endpoint->endpoint_id, (unsigned int)endpoint->cluster_count,
			(unsigned int)limits->max_clusters_per_endpoint);
		return -ENOSPC;
	}
	if (endpoint->clusters == NULL && endpoint->cluster_count > 0) {
		LOG_ERR("endpoint %u: cluster_count %u but clusters is NULL", endpoint->endpoint_id,
			(unsigned int)endpoint->cluster_count);
		return -EINVAL;
	}

	for (size_t c = 0; c < endpoint->cluster_count; c++) {
		const struct mcuhome_matter_cluster *cluster = &endpoint->clusters[c];

		if (cluster->id == MCUHOME_MATTER_DESCRIPTOR_CLUSTER_ID) {
			LOG_ERR("endpoint %u declares the Descriptor cluster; the framework "
				"appends it and derives its content from the tables",
				endpoint->endpoint_id);
			return -EINVAL;
		}
		for (size_t c2 = 0; c2 < c; c2++) {
			if (endpoint->clusters[c2].id == cluster->id) {
				LOG_ERR("endpoint %u: duplicate cluster id 0x%08x",
					endpoint->endpoint_id, cluster->id);
				return -EEXIST;
			}
		}
		attr_slots += cluster->attr_count + MCUHOME_MATTER_GLOBAL_ATTRS_PER_CLUSTER;

		if (cluster->attrs == NULL && cluster->attr_count > 0) {
			LOG_ERR("endpoint %u cluster 0x%08x: attr_count %u but attrs is NULL",
				endpoint->endpoint_id, cluster->id,
				(unsigned int)cluster->attr_count);
			return -EINVAL;
		}

		for (size_t a = 0; a < cluster->attr_count; a++) {
			int err = validate_attr(&cluster->attrs[a]);

			if (err != 0) {
				LOG_ERR("  ... in endpoint %u cluster 0x%08x",
					endpoint->endpoint_id, cluster->id);
				return err;
			}
			for (size_t a2 = 0; a2 < a; a2++) {
				if (cluster->attrs[a2].id == cluster->attrs[a].id) {
					LOG_ERR("endpoint %u cluster 0x%08x: duplicate attr "
						"id 0x%08x",
						endpoint->endpoint_id, cluster->id,
						cluster->attrs[a].id);
					return -EEXIST;
				}
			}
		}
	}

	if (attr_slots > limits->max_attrs_per_endpoint) {
		LOG_ERR("endpoint %u needs %u attribute slots, "
			"CONFIG_MCUHOME_MATTER_MAX_ATTRS_PER_ENDPOINT is %u",
			endpoint->endpoint_id, (unsigned int)attr_slots,
			(unsigned int)limits->max_attrs_per_endpoint);
		return -ENOSPC;
	}
	return 0;
}

/* Walk every endpoint's parent chain within the table. VERIFIED against
 * CHIP v1.5.1.0: emberAfEndpointEnableDisable()'s own parent walk
 * (src/app/util/attribute-storage.cpp:1011-1023) has no cycle guard and
 * infinite-loops under the held CHIP StackLock if a parent_id is
 * self-referential or forms a cycle with another dynamic endpoint — that
 * hangs the whole CHIP thread, not just the one endpoint. This layer
 * must make that state unreachable before it ever reaches ember. */
static int validate_parent_chains(const struct mcuhome_matter_node *node)
{
	for (size_t i = 0; i < node->endpoint_count; i++) {
		uint16_t current_id = node->endpoints[i].endpoint_id;
		uint16_t parent_id = node->endpoints[i].parent_id;
		size_t steps = 0;

		while (parent_id != 0) {
			if (parent_id == current_id) {
				LOG_ERR("endpoint %u: parent chain is self-referential "
					"(endpoint 0x%04x points back to itself)",
					node->endpoints[i].endpoint_id, current_id);
				return -EINVAL;
			}
			if (++steps > node->endpoint_count) {
				LOG_ERR("endpoint %u: parent chain exceeds %u steps (cycle)",
					node->endpoints[i].endpoint_id,
					(unsigned int)node->endpoint_count);
				return -EINVAL;
			}

			const struct mcuhome_matter_endpoint *parent = NULL;

			for (size_t k = 0; k < node->endpoint_count; k++) {
				if (node->endpoints[k].endpoint_id == parent_id) {
					parent = &node->endpoints[k];
					break;
				}
			}
			if (parent == NULL) {
				LOG_ERR("endpoint %u: parent_id %u is not a declared endpoint",
					node->endpoints[i].endpoint_id, parent_id);
				return -EINVAL;
			}

			current_id = parent->endpoint_id;
			parent_id = parent->parent_id;
		}
	}
	return 0;
}

int mcuhome_matter_validate_node(const struct mcuhome_matter_node *node,
				 const struct mcuhome_matter_limits *limits)
{
	if (node == NULL || node->endpoints == NULL) {
		LOG_ERR("no node configuration");
		return -EINVAL;
	}
	if (node->tables_version != MCUHOME_MATTER_TABLES_VERSION) {
		LOG_ERR("tables version %u, framework expects %u — regenerate the device "
			"configuration against this framework revision",
			node->tables_version, MCUHOME_MATTER_TABLES_VERSION);
		return -EINVAL;
	}
	if (node->endpoint_count > limits->max_endpoints) {
		LOG_ERR("%u endpoints exceed CONFIG_MCUHOME_MATTER_MAX_DYNAMIC_ENDPOINTS (%u)",
			(unsigned int)node->endpoint_count, (unsigned int)limits->max_endpoints);
		return -ENOSPC;
	}

	for (size_t i = 0; i < node->endpoint_count; i++) {
		int err = validate_endpoint(&node->endpoints[i], limits);

		if (err != 0) {
			return err;
		}
		for (size_t j = 0; j < i; j++) {
			if (node->endpoints[j].endpoint_id == node->endpoints[i].endpoint_id) {
				LOG_ERR("duplicate endpoint id %u", node->endpoints[i].endpoint_id);
				return -EEXIST;
			}
		}
	}

	return validate_parent_chains(node);
}
