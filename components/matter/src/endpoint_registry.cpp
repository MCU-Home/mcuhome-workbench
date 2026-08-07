/*
 * SPDX-FileCopyrightText: 2026 The MCUHome Contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * Translation layer between the MCUHome generated-tables contract
 * (<mcuhome/matter_tables.h>) and CHIP's ember data model (ADR 0014,
 * decision A). This file is the ONLY place in the tree that knows both
 * sides: everything above it sees plain C structs, everything below it
 * sees EmberAf* metadata.
 *
 * It also implements the node's single pair of external attribute
 * callbacks. Ember allows exactly one definition of each (they are plain
 * global C++ functions, not per-endpoint hooks), which is precisely why
 * they cannot live in application code once more than one component wants
 * attributes — moving them here is what makes the generated app glue
 * CHIP-free.
 *
 * Pure table validation (duplicate IDs, pool bounds, parent-chain cycles,
 * …) is NOT here: it lives in table_validate.c, a CHIP-free translation
 * unit that also builds standalone for the native_sim suite in
 * tests/matter_tables/, because CHIP cannot compile there. This file calls
 * it before translating a single table entry to ember metadata.
 */

#include <errno.h>
#include <string.h>

#include <array>
#include <utility>

#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/sys/atomic.h>
#include <zephyr/sys/util.h>

#include <app-common/zap-generated/ids/Clusters.h>
#include <app/reporting/reporting.h>
#include <app/util/attribute-storage.h>
#include <clusters/Descriptor/Metadata.h>
#include <lib/core/CHIPError.h>
#include <lib/core/DataModelTypes.h>
#include <lib/support/Span.h>
#include <platform/CHIPDeviceLayer.h>

#include <mcuhome/matter.h>
#include <mcuhome/matter_tables.h>

#include "matter_internal.h"
#include "table_validate.h"

LOG_MODULE_DECLARE(MCUHOME_MATTER_LOG_MODULE, CONFIG_LOG_DEFAULT_LEVEL);

using chip::AttributeId;
using chip::ClusterId;
using chip::DataVersion;
using chip::EndpointId;
using chip::Span;
using chip::Protocols::InteractionModel::Status;

/* The ember attribute store keeps scalars in host byte order, so the
 * external attribute callbacks below memcpy() raw little-endian words in
 * and out of the caller's buffer. On a big-endian target that is wrong and
 * would produce byte-swapped attribute values on the wire — fail the build
 * instead. (CHIP has CHIP_CONFIG_BIG_ENDIAN_TARGET for this; no MCUHome
 * target uses it today.) */
static_assert(__BYTE_ORDER__ == __ORDER_LITTLE_ENDIAN__,
	      "MCUHome's ember attribute translation assumes a little-endian target");

namespace
{

constexpr size_t kMaxEndpoints = CONFIG_MCUHOME_MATTER_MAX_DYNAMIC_ENDPOINTS;
constexpr size_t kMaxClusters = CONFIG_MCUHOME_MATTER_MAX_CLUSTERS_PER_ENDPOINT;
constexpr size_t kMaxAttrs = CONFIG_MCUHOME_MATTER_MAX_ATTRS_PER_ENDPOINT;
constexpr size_t kMaxDeviceTypes = CONFIG_MCUHOME_MATTER_MAX_DEVICE_TYPES_PER_ENDPOINT;

/* Spec-fixed global attribute IDs. The framework serves both for every
 * table cluster (ADR 0014); tables must not declare them. Their reserved
 * range (0xFFF8-0xFFFD) is rejected in table_validate.c. */
constexpr AttributeId kFeatureMapId = 0xFFFCu;
constexpr AttributeId kClusterRevisionId = 0xFFFDu;

/* --- Static translation pools -----------------------------------------
 *
 * Everything ember dereferences at runtime must outlive registration, so
 * all of it is static: no heap after init (AGENTS.md coding standards).
 * Sized by CONFIG_MCUHOME_MATTER_MAX_*; overflow is a hard init error, not
 * a truncation.
 *
 * EmberAfAttributeMetadata is NOT default-constructible: its defaultValue
 * member is a union that declares converting constructors only, which
 * deletes the implicit default one. CHIP never notices because it only
 * ever builds these arrays through the DECLARE_DYNAMIC_ATTRIBUTE* macros,
 * which spell out every element. Our pool is Kconfig-sized, so it gets a
 * compile-time-generated initializer instead — std::array plus the
 * pack-expansion filler below. */
template <typename T, size_t... Is>
constexpr std::array<T, sizeof...(Is)> FilledArray(const T &value, std::index_sequence<Is...>)
{
	return {{(static_cast<void>(Is), value)...}};
}

template <typename T, size_t N> constexpr std::array<T, N> FilledArray(const T &value)
{
	return FilledArray<T>(value, std::make_index_sequence<N>{});
}

constexpr EmberAfAttributeMetadata kEmptyAttrMeta{ZAP_EMPTY_DEFAULT(), 0u, 0u, 0u, 0u};

using AttrRow = std::array<EmberAfAttributeMetadata, kMaxAttrs>;

std::array<AttrRow, kMaxEndpoints> gAttrPool = FilledArray<AttrRow, kMaxEndpoints>(
	FilledArray<EmberAfAttributeMetadata, kMaxAttrs>(kEmptyAttrMeta));

EmberAfCluster gClusterPool[kMaxEndpoints][kMaxClusters];
EmberAfEndpointType gEndpointTypes[kMaxEndpoints];
DataVersion gDataVersions[kMaxEndpoints][kMaxClusters];
EmberAfDeviceType gDeviceTypePool[kMaxEndpoints][kMaxDeviceTypes];

/* The registered tables. Read by the external attribute callbacks from the
 * CHIP event loop; written once before that loop starts. */
const struct mcuhome_matter_node *gNode;

/* --- Deferred attribute change reports --------------------------------
 *
 * mcuhome_matter_attr_changed() may be called from any application thread,
 * so it must not touch the stack directly. ScheduleWork() carries a single
 * intptr_t, which cannot hold endpoint+cluster+attribute — hence a small
 * static slot pool whose address is what actually travels. */
struct PendingReport {
	uint16_t endpoint;
	uint32_t cluster;
	uint32_t attribute;
	atomic_t inUse;
};

PendingReport gPending[CONFIG_MCUHOME_MATTER_MAX_PENDING_REPORTS];

void ReportWork(intptr_t arg)
{
	PendingReport *pending = reinterpret_cast<PendingReport *>(arg);
	const EndpointId endpoint = static_cast<EndpointId>(pending->endpoint);
	const ClusterId cluster = static_cast<ClusterId>(pending->cluster);
	const AttributeId attr = static_cast<AttributeId>(pending->attribute);

	atomic_clear(&pending->inUse);
	MatterReportingAttributeChangeCallback(endpoint, cluster, attr);
}

/* --- Table lookup ------------------------------------------------------ */

const struct mcuhome_matter_endpoint *FindEndpoint(uint16_t endpointId)
{
	if (gNode == nullptr) {
		return nullptr;
	}
	for (size_t i = 0; i < gNode->endpoint_count; i++) {
		if (gNode->endpoints[i].endpoint_id == endpointId) {
			return &gNode->endpoints[i];
		}
	}
	return nullptr;
}

const struct mcuhome_matter_cluster *FindCluster(const struct mcuhome_matter_endpoint *endpoint,
						 uint32_t clusterId)
{
	for (size_t i = 0; i < endpoint->cluster_count; i++) {
		if (endpoint->clusters[i].id == clusterId) {
			return &endpoint->clusters[i];
		}
	}
	return nullptr;
}

const struct mcuhome_matter_attr *FindAttr(const struct mcuhome_matter_cluster *cluster,
					   uint32_t attrId)
{
	for (size_t i = 0; i < cluster->attr_count; i++) {
		if (cluster->attrs[i].id == attrId) {
			return &cluster->attrs[i];
		}
	}
	return nullptr;
}

/* --- Type mapping ------------------------------------------------------
 *
 * The one and only place where the contract's type enum meets CHIP's ZCL
 * attribute types. Adding a type means: enumerator in matter_tables.h,
 * a case here, and a tables-version bump. */
bool MapType(enum mcuhome_attr_type type, EmberAfAttributeType &emberType, uint16_t &size)
{
	switch (type) {
	case MCUHOME_ATTR_TYPE_INT16S:
		emberType = ZAP_TYPE(INT16S);
		size = 2;
		return true;
	case MCUHOME_ATTR_TYPE_INT16U:
		emberType = ZAP_TYPE(INT16U);
		size = 2;
		return true;
	case MCUHOME_ATTR_TYPE_INT32S:
		emberType = ZAP_TYPE(INT32S);
		size = 4;
		return true;
	case MCUHOME_ATTR_TYPE_INT32U:
		emberType = ZAP_TYPE(INT32U);
		size = 4;
		return true;
	case MCUHOME_ATTR_TYPE_BOOL:
		emberType = ZAP_TYPE(BOOLEAN);
		size = 1;
		return true;
	}
	return false;
}

/* Null encodings, taken from CHIP's NumericAttributeTraits
 * (src/app/util/attribute-storage-null-handling.h): the reserved marker is
 * the minimum for signed integers, the maximum for unsigned ones, and
 * 0xFF for booleans. Re-verify on a CHIP bump. */
uint32_t NullRaw(enum mcuhome_attr_type type)
{
	switch (type) {
	case MCUHOME_ATTR_TYPE_INT16S:
		return static_cast<uint32_t>(static_cast<uint16_t>(INT16_MIN));
	case MCUHOME_ATTR_TYPE_INT16U:
		return UINT16_MAX;
	case MCUHOME_ATTR_TYPE_INT32S:
		return static_cast<uint32_t>(INT32_MIN);
	case MCUHOME_ATTR_TYPE_INT32U:
		return UINT32_MAX;
	case MCUHOME_ATTR_TYPE_BOOL:
		return 0xFFu;
	}
	return 0;
}

uint32_t StoreRaw(const struct mcuhome_matter_attr *attr)
{
	const struct mcuhome_attr_store *store = attr->store;

	switch (attr->type) {
	case MCUHOME_ATTR_TYPE_INT16S:
		return static_cast<uint32_t>(static_cast<uint16_t>(store->v.i16s));
	case MCUHOME_ATTR_TYPE_INT16U:
		return store->v.u16;
	case MCUHOME_ATTR_TYPE_INT32S:
		return static_cast<uint32_t>(store->v.i32s);
	case MCUHOME_ATTR_TYPE_INT32U:
		return store->v.u32;
	case MCUHOME_ATTR_TYPE_BOOL:
		return store->v.b ? 1u : 0u;
	}
	return 0;
}

void RawToStore(const struct mcuhome_matter_attr *attr, uint32_t raw)
{
	struct mcuhome_attr_store *store = attr->store;

	switch (attr->type) {
	case MCUHOME_ATTR_TYPE_INT16S:
		store->v.i16s = static_cast<int16_t>(static_cast<uint16_t>(raw));
		break;
	case MCUHOME_ATTR_TYPE_INT16U:
		store->v.u16 = static_cast<uint16_t>(raw);
		break;
	case MCUHOME_ATTR_TYPE_INT32S:
		store->v.i32s = static_cast<int32_t>(raw);
		break;
	case MCUHOME_ATTR_TYPE_INT32U:
		store->v.u32 = raw;
		break;
	case MCUHOME_ATTR_TYPE_BOOL:
		store->v.b = (raw != 0);
		break;
	}
}

Status PutRaw(uint8_t *buffer, uint16_t maxLength, uint16_t size, uint32_t raw)
{
	if (buffer == nullptr || maxLength < size) {
		return Status::ResourceExhausted;
	}
	memcpy(buffer, &raw, size);
	return Status::Success;
}

uint32_t GetRaw(const uint8_t *buffer, uint16_t size)
{
	uint32_t raw = 0;

	memcpy(&raw, buffer, size);
	return raw;
}

/* --- Translation ------------------------------------------------------- */

EmberAfAttributeMetadata MakeAttrMeta(AttributeId id, EmberAfAttributeType type, uint16_t size,
				      EmberAfAttributeMask extraMask)
{
	/* Every attribute is externally stored: the values live in the
	 * contract's attr_store cells (or are constants), never in ember's
	 * own attribute store. READABLE mirrors DECLARE_DYNAMIC_ATTRIBUTE(),
	 * which adds it unconditionally. */
	const EmberAfAttributeMask mask = static_cast<EmberAfAttributeMask>(
		extraMask | MATTER_ATTRIBUTE_FLAG_EXTERNAL_STORAGE |
		MATTER_ATTRIBUTE_FLAG_READABLE);

	return EmberAfAttributeMetadata{ZAP_EMPTY_DEFAULT(), id, size, type, mask};
}

EmberAfCluster MakeCluster(ClusterId id, const EmberAfAttributeMetadata *attrs, uint16_t attrCount)
{
	return EmberAfCluster{
		id,
		attrs,
		attrCount,
		/* clusterSize   */ 0, /* all attributes are external */
		/* mask          */ MATTER_CLUSTER_FLAG_SERVER,
		/* functions     */ nullptr,
		/* accepted cmds */ nullptr,
		/* generated cmds*/ nullptr,
		/* eventList     */ nullptr,
		/* eventCount    */ 0,
	};
}

/* Build the ember metadata for one endpoint into pool slot @p index. */
void TranslateEndpoint(size_t index, const struct mcuhome_matter_endpoint *endpoint)
{
	size_t attrCursor = 0;
	size_t clusterCursor = 0;

	for (size_t c = 0; c < endpoint->cluster_count; c++) {
		const struct mcuhome_matter_cluster *cluster = &endpoint->clusters[c];
		EmberAfAttributeMetadata *attrs = &gAttrPool[index][attrCursor];
		uint16_t attrCount = 0;

		for (size_t a = 0; a < cluster->attr_count; a++) {
			const struct mcuhome_matter_attr *attr = &cluster->attrs[a];
			EmberAfAttributeType emberType = 0;
			uint16_t size = 0;
			EmberAfAttributeMask extra = 0;

			(void)MapType(attr->type, emberType, size); /* validated above */
			if ((attr->flags & MCUHOME_ATTR_F_WRITABLE) != 0) {
				extra |= MATTER_ATTRIBUTE_FLAG_WRITABLE;
			}
			if ((attr->flags & MCUHOME_ATTR_F_NULLABLE) != 0) {
				extra |= MATTER_ATTRIBUTE_FLAG_NULLABLE;
			}
			gAttrPool[index][attrCursor++] =
				MakeAttrMeta(attr->id, emberType, attr->size, extra);
			attrCount++;
		}

		/* FeatureMap is NOT auto-appended by ember's DECLARE_* macros —
		 * the prototype therefore answered UNSUPPORTED_ATTRIBUTE for it
		 * on its dynamic cluster. Declare it explicitly; the read
		 * callback serves it from cluster->feature_map (ADR 0014). */
		gAttrPool[index][attrCursor++] =
			MakeAttrMeta(kFeatureMapId, ZAP_TYPE(INT32U), 4, 0);
		attrCount++;

		/* ClusterRevision is what DECLARE_DYNAMIC_ATTRIBUTE_LIST_END()
		 * appends; replicated explicitly because we build the metadata
		 * arrays at runtime instead of with the macros. Served from
		 * cluster->cluster_revision (the prototype had no backing value
		 * at all and answered FAILURE). */
		gAttrPool[index][attrCursor++] =
			MakeAttrMeta(kClusterRevisionId, ZAP_TYPE(INT16U), 2, 0);
		attrCount++;

		gClusterPool[index][clusterCursor++] = MakeCluster(cluster->id, attrs, attrCount);
	}

	/* Append the Descriptor cluster.
	 *
	 * VERIFIED against CHIP v1.5.1.0: the Descriptor cluster is
	 * code-driven (a ServerClusterInterface in
	 * src/app/clusters/descriptor/). Its DeviceTypeList / ServerList /
	 * ClientList / PartsList are computed from the endpoint metadata, so
	 * the four list attributes the prototype declared are vestigial and
	 * are dropped here. What is NOT vestigial is the cluster ENTRY
	 * itself: initializeEndpoint() walks the cluster metadata and calls
	 * MatterClusterServerInitCallback() per entry, which is what
	 * instantiates the Descriptor server — without the entry the endpoint
	 * has no Descriptor at all and controllers see an unusable device.
	 *
	 * VERSION COUPLING: this split (entry load-bearing, attributes
	 * vestigial) is an SDK-internal detail. Re-verify it on every CHIP
	 * bump — src/app/clusters/descriptor/CodegenIntegration.cpp and
	 * src/app/util/attribute-storage.cpp. */
	EmberAfAttributeMetadata *descriptorAttrs = &gAttrPool[index][attrCursor];

	gAttrPool[index][attrCursor++] = MakeAttrMeta(kClusterRevisionId, ZAP_TYPE(INT16U), 2, 0);
	gClusterPool[index][clusterCursor++] =
		MakeCluster(chip::app::Clusters::Descriptor::Id, descriptorAttrs,
			    static_cast<uint16_t>(MCUHOME_MATTER_DESCRIPTOR_ATTR_COUNT));

	for (size_t d = 0; d < endpoint->device_type_count; d++) {
		gDeviceTypePool[index][d] = EmberAfDeviceType{
			static_cast<chip::DeviceTypeId>(endpoint->device_types[d].id),
			endpoint->device_types[d].revision,
		};
	}

	gEndpointTypes[index] = EmberAfEndpointType{
		gClusterPool[index],
		static_cast<uint8_t>(clusterCursor),
		/* endpointSize */ 0, /* all attributes are external */
	};
}

} /* namespace */

/* --- External attribute callbacks --------------------------------------
 *
 * THE node-wide pair. Ember resolves these as ordinary global symbols, so
 * exactly one definition may exist in the image; the sample used to carry
 * its own and they were removed in the same change that added these. */

Status emberAfExternalAttributeReadCallback(EndpointId endpoint, ClusterId clusterId,
					    const EmberAfAttributeMetadata *attributeMetadata,
					    uint8_t *buffer, uint16_t maxReadLength)
{
	const struct mcuhome_matter_endpoint *ep = FindEndpoint(endpoint);

	if (ep == nullptr) {
		return Status::UnsupportedEndpoint;
	}

	const struct mcuhome_matter_cluster *cluster = FindCluster(ep, clusterId);

	if (cluster == nullptr) {
		/* Only the appended Descriptor cluster can land here, and only
		 * if the code-driven Descriptor server was not registered (see
		 * TranslateEndpoint). Answer its ClusterRevision from the SDK's
		 * own constant rather than returning FAILURE. */
		if (clusterId == chip::app::Clusters::Descriptor::Id &&
		    attributeMetadata->attributeId == kClusterRevisionId) {
			return PutRaw(buffer, maxReadLength, 2,
				      chip::app::Clusters::Descriptor::kRevision);
		}
		return Status::UnsupportedCluster;
	}

	if (attributeMetadata->attributeId == kFeatureMapId) {
		return PutRaw(buffer, maxReadLength, 4, cluster->feature_map);
	}
	if (attributeMetadata->attributeId == kClusterRevisionId) {
		return PutRaw(buffer, maxReadLength, 2, cluster->cluster_revision);
	}

	const struct mcuhome_matter_attr *attr = FindAttr(cluster, attributeMetadata->attributeId);

	if (attr == nullptr) {
		return Status::UnsupportedAttribute;
	}

	if (attr->store != nullptr && __atomic_load_n(&attr->store->valid, __ATOMIC_ACQUIRE)) {
		return PutRaw(buffer, maxReadLength, attr->size, StoreRaw(attr));
	}
	/* No value yet (or a constant attribute). A nullable attribute
	 * reports null rather than a plausible-looking default — "no reading
	 * yet" and "reading is 0" must not look the same to a controller. */
	if (attr->store != nullptr && (attr->flags & MCUHOME_ATTR_F_NULLABLE) != 0) {
		return PutRaw(buffer, maxReadLength, attr->size, NullRaw(attr->type));
	}
	return PutRaw(buffer, maxReadLength, attr->size, static_cast<uint32_t>(attr->def));
}

Status emberAfExternalAttributeWriteCallback(EndpointId endpoint, ClusterId clusterId,
					     const EmberAfAttributeMetadata *attributeMetadata,
					     uint8_t *buffer)
{
	const struct mcuhome_matter_endpoint *ep = FindEndpoint(endpoint);

	if (ep == nullptr) {
		return Status::UnsupportedEndpoint;
	}

	const struct mcuhome_matter_cluster *cluster = FindCluster(ep, clusterId);

	if (cluster == nullptr) {
		return Status::UnsupportedCluster;
	}
	/* FeatureMap and ClusterRevision are read-only by specification. */
	if (attributeMetadata->attributeId == kFeatureMapId ||
	    attributeMetadata->attributeId == kClusterRevisionId) {
		return Status::UnsupportedWrite;
	}

	const struct mcuhome_matter_attr *attr = FindAttr(cluster, attributeMetadata->attributeId);

	if (attr == nullptr) {
		return Status::UnsupportedAttribute;
	}
	if ((attr->flags & MCUHOME_ATTR_F_WRITABLE) == 0 || attr->store == nullptr) {
		return Status::UnsupportedWrite;
	}
	if (buffer == nullptr) {
		return Status::Failure;
	}

	const uint32_t raw = GetRaw(buffer, attr->size);

	/* Writing the null marker of a nullable attribute invalidates the
	 * cell instead of storing the marker as a value. */
	if ((attr->flags & MCUHOME_ATTR_F_NULLABLE) != 0 && raw == NullRaw(attr->type)) {
		mcuhome_attr_store_invalidate(attr->store);
		return Status::Success;
	}

	RawToStore(attr, raw);
	__atomic_store_n(&attr->store->valid, true, __ATOMIC_RELEASE);
	return Status::Success;
}

/* --- Framework-internal entry points ----------------------------------- */

int mcuhome_matter_registry_register(const struct mcuhome_matter_node *node)
{
	const struct mcuhome_matter_limits limits = {kMaxEndpoints, kMaxClusters, kMaxAttrs,
						     kMaxDeviceTypes};
	int err = mcuhome_matter_validate_node(node, &limits);

	if (err != 0) {
		return err;
	}

	gNode = node;

	for (size_t i = 0; i < node->endpoint_count; i++) {
		const struct mcuhome_matter_endpoint *endpoint = &node->endpoints[i];
		CHIP_ERROR chipErr;

		TranslateEndpoint(i, endpoint);

		{
			/* emberAfSetDynamicEndpoint() mutates the endpoint
			 * registry the CHIP event loop reads. */
			chip::DeviceLayer::StackLock lock;

			chipErr = emberAfSetDynamicEndpoint(
				static_cast<uint16_t>(i),
				static_cast<EndpointId>(endpoint->endpoint_id), &gEndpointTypes[i],
				Span<DataVersion>(gDataVersions[i], gEndpointTypes[i].clusterCount),
				Span<const EmberAfDeviceType>(gDeviceTypePool[i],
							      endpoint->device_type_count),
				static_cast<EndpointId>(endpoint->parent_id));

			if (chipErr != CHIP_NO_ERROR) {
				LOG_ERR("endpoint %u registration failed: %" CHIP_ERROR_FORMAT,
					endpoint->endpoint_id, chipErr.Format());

				/* Partial ember registration must never outlive a
				 * failed start: endpoints [0, i) are already live in
				 * ember's dynamic endpoint table, and the CHIP event
				 * loop's external attribute callbacks resolve
				 * endpoints by walking gNode — if we returned with
				 * gNode cleared but those entries still registered,
				 * a controller talking to a "failed" node would get
				 * UnsupportedEndpoint for endpoints ember still
				 * believes are live, instead of the node simply not
				 * being there. Unwind them here, still under the
				 * same lock that made them visible. */
				for (size_t j = 0; j < i; j++) {
					emberAfClearDynamicEndpoint(static_cast<uint16_t>(j));
				}
				gNode = nullptr;
				return -EIO;
			}
		}
		LOG_INF("endpoint %u registered (parent %u, %u clusters incl. Descriptor)",
			endpoint->endpoint_id, endpoint->parent_id, gEndpointTypes[i].clusterCount);
	}

	return 0;
}

/* --- Public lookup API (<mcuhome/matter.h>) ----------------------------- */

const struct mcuhome_attr_store *
mcuhome_matter_attr_store_lookup(uint16_t endpoint_id, uint32_t cluster_id, uint32_t attr_id)
{
	const struct mcuhome_matter_endpoint *ep = FindEndpoint(endpoint_id);

	if (ep == nullptr) {
		return nullptr;
	}

	const struct mcuhome_matter_cluster *cluster = FindCluster(ep, cluster_id);

	if (cluster == nullptr) {
		return nullptr;
	}

	const struct mcuhome_matter_attr *attr = FindAttr(cluster, attr_id);

	if (attr == nullptr) {
		return nullptr;
	}

	return attr->store;
}

/* --- Framework-internal entry points ----------------------------------- */

void mcuhome_matter_registry_report(uint16_t endpoint_id, uint32_t cluster_id, uint32_t attr_id)
{
	for (size_t i = 0; i < ARRAY_SIZE(gPending); i++) {
		if (!atomic_cas(&gPending[i].inUse, 0, 1)) {
			continue;
		}

		gPending[i].endpoint = endpoint_id;
		gPending[i].cluster = cluster_id;
		gPending[i].attribute = attr_id;

		CHIP_ERROR err = chip::DeviceLayer::PlatformMgr().ScheduleWork(
			ReportWork, reinterpret_cast<intptr_t>(&gPending[i]));

		if (err != CHIP_NO_ERROR) {
			atomic_clear(&gPending[i].inUse);
			LOG_WRN("change report for ep %u cluster 0x%08x attr 0x%08x not "
				"scheduled: %" CHIP_ERROR_FORMAT,
				endpoint_id, cluster_id, attr_id, err.Format());
		}
		return;
	}

	LOG_WRN("change report for ep %u cluster 0x%08x attr 0x%08x dropped: all %u slots busy "
		"(raise CONFIG_MCUHOME_MATTER_MAX_PENDING_REPORTS)",
		endpoint_id, cluster_id, attr_id, (unsigned int)ARRAY_SIZE(gPending));
}
