/*
 * SPDX-FileCopyrightText: 2026 The MCUHome Contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * MCUHome generated-tables contract for Matter (ADR 0014).
 *
 * This header is the ONLY interface between generated device configuration
 * and the Matter runtime. It is deliberately plain C and contains zero
 * CHIP/ember types, so a device config emitted by the builder is dumb,
 * reviewable, diffable data that survives Matter SDK version bumps without
 * regeneration.
 *
 * Ownership rules (all of them load-bearing):
 *
 * - The tables themselves (node/endpoint/cluster/attr/device-type structs)
 *   are `const` and live in flash. The framework never writes to them.
 * - `struct mcuhome_attr_store` cells are RAM and owned by application /
 *   channel code: it publishes the value with one of the
 *   mcuhome_attr_store_publish_*() helpers below (or clears it with
 *   mcuhome_attr_store_invalidate()) and then calls
 *   mcuhome_matter_attr_changed() (see <mcuhome/matter.h>). The framework
 *   only reads them — except through the Matter write path, where it writes
 *   a cell on behalf of a controller (attributes flagged writable), using
 *   the same helpers internally.
 *   PUBLISH VIA THE HELPERS ONLY. The cell is written by an application
 *   thread and read by the CHIP thread with no other synchronization
 *   between them; a direct `store->v.xxx = val; store->valid = true;`
 *   sequence is a C11 data race — `valid` can become visible to the reader
 *   before `v` does. The helpers use GCC/Clang `__atomic` builtins on
 *   `valid` (release on publish, acquire on read) to fix the ordering
 *   without changing this struct's layout.
 * - ALL interaction with CHIP/ember — metadata translation, endpoint
 *   registration, external attribute callbacks, DataVersions, stack locking
 *   — belongs to components/matter. Application code never includes a CHIP
 *   header and never touches CHIP's locking.
 * - No heap: the framework translates these tables into statically sized
 *   pools (CONFIG_MCUHOME_MATTER_MAX_* in components/matter/Kconfig) during
 *   init and allocates nothing afterwards.
 *
 * Two things a table author (and the builder) must NOT declare:
 *
 * - The Descriptor cluster. The framework appends it to every endpoint and
 *   derives its content from the tables (ADR 0014, decision B).
 * - Any global attribute (0xFFF8..0xFFFD). FeatureMap (0xFFFC) and
 *   ClusterRevision (0xFFFD) are served by the framework from the cluster's
 *   `feature_map` / `cluster_revision` fields; the attribute/command lists
 *   (0xFFF8..0xFFFB) are appended by CHIP's data-model provider itself —
 *   declaring them here would duplicate wire-visible list entries.
 *
 * Contract changes bump MCUHOME_MATTER_TABLES_VERSION; the framework checks
 * it at init so a stale generator/framework pairing fails loudly instead of
 * misbehaving silently.
 */

#ifndef MCUHOME_MATTER_TABLES_H_
#define MCUHOME_MATTER_TABLES_H_

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/** Version of the tables contract this header defines. */
#define MCUHOME_MATTER_TABLES_VERSION 1

/**
 * @brief Attribute value types supported by contract v1.
 *
 * Deliberately minimal: the scalar types the phase-1 component set needs.
 * The mapping to CHIP's ZAP_TYPE/ZCL_*_ATTRIBUTE_TYPE values lives inside
 * the framework (components/matter/src/endpoint_registry.cpp) and nowhere
 * else — that indirection is the whole point of this file. Extend by
 * appending new enumerators (never renumbering) and bumping
 * MCUHOME_MATTER_TABLES_VERSION.
 */
enum mcuhome_attr_type {
	MCUHOME_ATTR_TYPE_INT16S = 0,
	MCUHOME_ATTR_TYPE_INT16U = 1,
	MCUHOME_ATTR_TYPE_INT32S = 2,
	MCUHOME_ATTR_TYPE_INT32U = 3,
	MCUHOME_ATTR_TYPE_BOOL = 4,
};

/** Attribute is writable by a Matter controller. Requires a `store` cell. */
#define MCUHOME_ATTR_F_WRITABLE 0x01U
/** Attribute is nullable: an invalid store cell is reported as null. */
#define MCUHOME_ATTR_F_NULLABLE 0x02U

/**
 * @brief RAM cell backing one attribute value.
 *
 * Written by application/channel code (or by the framework on a controller
 * write), read by the framework when a controller reads the attribute.
 *
 * `valid` is the "do we have a measurement yet?" flag. If it is false the
 * framework reports null for nullable attributes and the table's `def`
 * value for non-nullable ones — a sensor that has not produced a reading
 * yet must never report a plausible-looking zero.
 *
 * Do not write `v` or `valid` directly outside this header — use the
 * mcuhome_attr_store_publish_*() / mcuhome_attr_store_invalidate() helpers
 * below (see the ownership rules above for why).
 */
struct mcuhome_attr_store {
	union {
		int16_t i16s;
		uint16_t u16;
		int32_t i32s;
		uint32_t u32;
		bool b;
	} v;
	bool valid;
};

/**
 * @name Attribute store publication helpers
 *
 * The only supported way to write a `struct mcuhome_attr_store` cell after
 * it was zero-initialized. Each stores the payload into the union member
 * matching its own type and then publishes `valid` with a release store;
 * paired with the acquire load the framework's external attribute read
 * callback (components/matter/src/endpoint_registry.cpp) uses, this
 * guarantees a reader that observes `valid == true` also observes the value
 * written here, not a stale or torn one.
 *
 * One helper per union member actually in use by contract v1 — extend
 * alongside `enum mcuhome_attr_type` if a new one is ever added.
 *
 * MCUHOME_MATTER_TABLES_VERSION deliberately stays 1 for the addition of
 * these helpers: the project is pre-release, no consumer of this header
 * exists yet outside this tree, and there is no ABI to break. From this
 * point on, though, the helpers ARE the contract for touching a store cell
 * — not the struct fields.
 * @{
 */
static inline void mcuhome_attr_store_publish_i16s(struct mcuhome_attr_store *store, int16_t val)
{
	store->v.i16s = val;
	__atomic_store_n(&store->valid, true, __ATOMIC_RELEASE);
}

static inline void mcuhome_attr_store_publish_u16(struct mcuhome_attr_store *store, uint16_t val)
{
	store->v.u16 = val;
	__atomic_store_n(&store->valid, true, __ATOMIC_RELEASE);
}

static inline void mcuhome_attr_store_publish_i32s(struct mcuhome_attr_store *store, int32_t val)
{
	store->v.i32s = val;
	__atomic_store_n(&store->valid, true, __ATOMIC_RELEASE);
}

static inline void mcuhome_attr_store_publish_u32(struct mcuhome_attr_store *store, uint32_t val)
{
	store->v.u32 = val;
	__atomic_store_n(&store->valid, true, __ATOMIC_RELEASE);
}

static inline void mcuhome_attr_store_publish_b(struct mcuhome_attr_store *store, bool val)
{
	store->v.b = val;
	__atomic_store_n(&store->valid, true, __ATOMIC_RELEASE);
}

/**
 * @brief Clear an attribute's store cell back to "no value yet".
 *
 * Same release-store ordering as the publish helpers above, applied to
 * `valid` alone.
 */
static inline void mcuhome_attr_store_invalidate(struct mcuhome_attr_store *store)
{
	__atomic_store_n(&store->valid, false, __ATOMIC_RELEASE);
}
/** @} */

/**
 * @brief One attribute of one cluster.
 *
 * `store == NULL` marks a constant attribute: the framework always serves
 * `def`, and the attribute can never be written. This is how fixed cluster
 * metadata such as MinMeasuredValue / MaxMeasuredValue is expressed without
 * spending RAM on it.
 */
struct mcuhome_matter_attr {
	/** Matter attribute ID (never 0xFFF8..0xFFFD, see file comment). */
	uint32_t id;
	enum mcuhome_attr_type type;
	/** Wire size in bytes; must match `type`. Validated at init. */
	uint16_t size;
	/** MCUHOME_ATTR_F_* bit mask. */
	uint8_t flags;
	/** RAM cell, or NULL for a constant attribute. */
	struct mcuhome_attr_store *store;
	/** Constant value, and the fallback for an invalid non-nullable cell. */
	int32_t def;
};

/** One entry of an endpoint's DeviceTypeList. */
struct mcuhome_matter_device_type {
	uint32_t id;
	uint8_t revision;
};

/**
 * @brief One server cluster of an endpoint.
 *
 * Never the Descriptor cluster (0x001D) — the framework appends it.
 */
struct mcuhome_matter_cluster {
	uint32_t id;
	/** Served as FeatureMap (0xFFFC) by the framework. */
	uint32_t feature_map;
	/** Served as ClusterRevision (0xFFFD) by the framework. */
	uint16_t cluster_revision;
	const struct mcuhome_matter_attr *attrs;
	size_t attr_count;
};

/**
 * @brief One application endpoint.
 *
 * `endpoint_id` is >= 1 and unique across the node: endpoint 0 is the root
 * node and the only statically compiled endpoint (ADR 0014, decision B).
 * `parent_id == 0` places the endpoint directly under the root, which is
 * the normal case for a composed device.
 */
struct mcuhome_matter_endpoint {
	uint16_t endpoint_id;
	uint16_t parent_id;
	const struct mcuhome_matter_device_type *device_types;
	size_t device_type_count;
	const struct mcuhome_matter_cluster *clusters;
	size_t cluster_count;
};

/**
 * @brief The whole generated device configuration.
 *
 * Generated code emits exactly one instance of this, named
 * `mcuhome_node_config`, and nothing else.
 */
struct mcuhome_matter_node {
	/** Must be MCUHOME_MATTER_TABLES_VERSION. Checked at init. */
	uint16_t tables_version;
	const struct mcuhome_matter_endpoint *endpoints;
	size_t endpoint_count;
};

#ifdef __cplusplus
}
#endif

#endif /* MCUHOME_MATTER_TABLES_H_ */
