/*
 * SPDX-FileCopyrightText: 2026 The MCUHome Contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * CHIP-free validation of the generated-tables contract (ADR 0014,
 * <mcuhome/matter_tables.h>). Declares the entry point implemented by
 * table_validate.c, which touches nothing but plain-C contract structs —
 * no CHIP/ember header, no Zephyr kernel API. That is what makes it
 * buildable and testable on native_sim, where CHIP cannot compile
 * (tests/matter_tables/).
 *
 * endpoint_registry.cpp is the only framework caller: it runs this before
 * translating a single table entry to ember metadata, then supplies its
 * own Kconfig-derived pool limits.
 */

#ifndef MCUHOME_COMPONENTS_MATTER_TABLE_VALIDATE_H_
#define MCUHOME_COMPONENTS_MATTER_TABLE_VALIDATE_H_

#include <stddef.h>

#include <mcuhome/matter_tables.h>

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief Pool bounds the translation layer sizes its static pools to.
 *
 * Mirrors CONFIG_MCUHOME_MATTER_MAX_* (components/matter/Kconfig) one for
 * one. Passed in as plain fields, rather than read from Kconfig directly,
 * so tests can exercise bounds without Kconfig coupling.
 */
struct mcuhome_matter_limits {
	/** CONFIG_MCUHOME_MATTER_MAX_DYNAMIC_ENDPOINTS. */
	size_t max_endpoints;
	/**
	 * CONFIG_MCUHOME_MATTER_MAX_CLUSTERS_PER_ENDPOINT, INCLUDING the
	 * Descriptor cluster slot the framework appends to every endpoint.
	 */
	size_t max_clusters_per_endpoint;
	/**
	 * CONFIG_MCUHOME_MATTER_MAX_ATTRS_PER_ENDPOINT, INCLUDING the global
	 * attribute slots (FeatureMap + ClusterRevision per declared
	 * cluster) and the appended Descriptor cluster's own ClusterRevision
	 * slot (MCUHOME_MATTER_DESCRIPTOR_ATTR_COUNT below).
	 */
	size_t max_attrs_per_endpoint;
	/** CONFIG_MCUHOME_MATTER_MAX_DEVICE_TYPES_PER_ENDPOINT. */
	size_t max_device_types_per_endpoint;
};

/**
 * @brief Attribute slots the framework-appended Descriptor cluster costs.
 *
 * One: its auto ClusterRevision entry (ADR 0014, decision B — the four
 * list attributes a hand-authored ZAP would carry are vestigial and never
 * declared here). Shared with endpoint_registry.cpp's translation layer,
 * which builds that same cluster's ember metadata, so the two stay in
 * lockstep by construction instead of by convention.
 */
#define MCUHOME_MATTER_DESCRIPTOR_ATTR_COUNT 1u

/**
 * @brief Validate a generated device configuration against the contract.
 *
 * Runs before a single ember structure is touched (see
 * mcuhome_matter_registry_register() in matter_internal.h): a
 * half-registered node is far harder to diagnose than a refused one.
 * Checks tables_version, structural well-formedness (NULL arrays with a
 * nonzero count), duplicate IDs at every level, the framework-owned
 * global attribute range (0xFFF8-0xFFFD), the framework-owned Descriptor
 * cluster, pool bounds, and every endpoint's parent chain (self-reference
 * and cross-endpoint cycles) — see ADR 0014.
 *
 * @param node Candidate device configuration. May be NULL.
 * @param limits Pool bounds the caller's translation layer sizes its
 *               static pools to. Must not be NULL.
 *
 * @retval 0 @p node is well-formed and fits within @p limits.
 * @retval -EINVAL @p node (or its endpoints array) is NULL,
 *                 `tables_version` does not match
 *                 MCUHOME_MATTER_TABLES_VERSION, or a table entry is
 *                 structurally malformed (bad endpoint ID, unknown
 *                 attribute type, a NULL array with a nonzero count, a
 *                 broken parent chain, …).
 * @retval -EEXIST Two entries at the same level (endpoints, clusters of
 *                 one endpoint, or attrs of one cluster) share an ID, or
 *                 an endpoint ID collides with the static root endpoint.
 * @retval -ENOSPC A pool in @p limits is too small for @p node.
 */
int mcuhome_matter_validate_node(const struct mcuhome_matter_node *node,
				 const struct mcuhome_matter_limits *limits);

#ifdef __cplusplus
}
#endif

#endif /* MCUHOME_COMPONENTS_MATTER_TABLE_VALIDATE_H_ */
