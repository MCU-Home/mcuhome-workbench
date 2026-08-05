/*
 * SPDX-FileCopyrightText: 2026 The MCUHome Contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * Matter bring-up: the stage sequence that was proven end to end on real
 * hardware in the phase-1 prototype, lifted out of the sample application
 * and into the framework (ADR 0014). The stage ORDER is load-bearing —
 * every step here failed in some instructive way while it was being
 * established; see docs/design/matter-zephyr-integration.md.
 */

#include <errno.h>

#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/toolchain.h>

#include <app/server/Server.h>
#include <app/util/attribute-storage.h>
#include <credentials/DeviceAttestationCredsProvider.h>
#include <credentials/examples/DeviceAttestationCredsExample.h>
#include <data-model-providers/codegen/Instance.h>
#include <lib/core/CHIPError.h>
#include <lib/support/CHIPMem.h>
#include <platform/CHIPDeviceLayer.h>

#include <mcuhome/matter.h>

#include "matter_internal.h"

LOG_MODULE_REGISTER(MCUHOME_MATTER_LOG_MODULE, CONFIG_LOG_DEFAULT_LEVEL);

/* --- Build-time guards -------------------------------------------------
 *
 * The `matter` snippet carries the numeric Kconfig values the CHIP stack
 * needs; vanilla Zephyr's defaults are far below them and a build without
 * the snippet dies at runtime (pre-main, or mid-PASE) with no console to
 * explain why. Fail the build instead — loudly, naming the snippet. */
BUILD_ASSERT(CONFIG_MAIN_STACK_SIZE >= 8192,
	     "CONFIG_MAIN_STACK_SIZE is too small for the Matter stack. "
	     "Build with the framework snippet: -S matter");

#ifdef CONFIG_MBEDTLS_HEAP_SIZE
BUILD_ASSERT(CONFIG_MBEDTLS_HEAP_SIZE >= 15360,
	     "CONFIG_MBEDTLS_HEAP_SIZE is too small: the first real ECC operation fails with "
	     "PSA_ERROR_INSUFFICIENT_MEMORY and the node never registers with the SRP server. "
	     "Build with the framework snippet: -S matter");
#endif

/* MCUHome devices are native composed nodes, not bridges (ADR 0014): the
 * framework ZAP (components/matter/zap/mcuhome-root.zap) defines endpoint 0
 * — the root node — and nothing else. Every application endpoint is created
 * at runtime from the generated tables, starting at EP1 directly under the
 * root; there is no static aggregator endpoint.
 *
 * A statically compiled endpoint would collide with the IDs the dynamic
 * allocator hands out and show up to controllers as a device the user never
 * configured. Fail the build, not the commissioning, if the ZAP ever grows
 * one. (FIXED_ENDPOINT_COUNT comes from the generated
 * zap-generated/endpoint_config.h, pulled in by app/util/attribute-storage.h
 * above — no fragile include path needed.) */
static_assert(FIXED_ENDPOINT_COUNT == 1,
	      "The MCUHome framework ZAP must define endpoint 0 only — "
	      "static endpoints collide with dynamically registered ones");

#if !defined(CONFIG_MCUHOME_MATTER_TEST_DAC)
#error "No device attestation credentials provider is configured. " \
	"CONFIG_MCUHOME_MATTER_TEST_DAC=n is only valid once MCUHome's own attestation " \
	"root is wired up (ADR 0012); until then a node without a DAC provider fails " \
	"commissioning at the attestation step with no useful diagnostic."
#endif

/* Retry pacing for CONFIG_MCUHOME_MATTER_INIT_RETRY_FOREVER. Long enough
 * that a failing stage does not flood the log, short enough that a node
 * whose network came up late recovers on its own. */
#define MCUHOME_STAGE_RETRY_DELAY_S 15

/* Report one stage outcome and, on failure, either retry forever or bail
 * out of mcuhome_matter_start(). Expands inside that function only. */
#define MCUHOME_STAGE(stage_name, expr)                                                            \
	do {                                                                                       \
		CHIP_ERROR _err;                                                                   \
		while ((_err = (expr)) != CHIP_NO_ERROR) {                                         \
			LOG_ERR("stage %s failed: %" CHIP_ERROR_FORMAT, stage_name,                \
				_err.Format());                                                    \
			mcuhome_matter_stage(stage_name, static_cast<int>(_err.AsInteger()));      \
			if (!IS_ENABLED(CONFIG_MCUHOME_MATTER_INIT_RETRY_FOREVER)) {               \
				return -EIO;                                                       \
			}                                                                          \
			k_sleep(K_SECONDS(MCUHOME_STAGE_RETRY_DELAY_S));                           \
		}                                                                                  \
		LOG_INF("stage %s ok", stage_name);                                                \
		mcuhome_matter_stage(stage_name, 0);                                               \
	} while (0)

/* Same, for stages that fail with an errno instead of a CHIP_ERROR. These
 * are framework-side failures (table validation, pool sizing) — never
 * retried, because they cannot fix themselves. */
#define MCUHOME_STAGE_ERRNO(stage_name, expr)                                                      \
	do {                                                                                       \
		int _rc = (expr);                                                                  \
		mcuhome_matter_stage(stage_name, _rc);                                             \
		if (_rc != 0) {                                                                    \
			LOG_ERR("stage %s failed: %d", stage_name, _rc);                           \
			return _rc;                                                                \
		}                                                                                  \
		LOG_INF("stage %s ok", stage_name);                                                \
	} while (0)

__weak void mcuhome_matter_stage(const char *name, int err)
{
	if (err == 0) {
		LOG_DBG("stage %s complete", name);
	} else {
		LOG_WRN("stage %s reported error %d", name, err);
	}
}

int mcuhome_matter_start(const struct mcuhome_matter_node *node)
{
	if (node == nullptr) {
		LOG_ERR("no node configuration");
		return -EINVAL;
	}

	MCUHOME_STAGE("MemoryInit", chip::Platform::MemoryInit());
	MCUHOME_STAGE("InitChipStack", chip::DeviceLayer::PlatformMgr().InitChipStack());

#ifdef CONFIG_NET_L2_OPENTHREAD
	/* Attach CHIP's ThreadStackManager to the (Zephyr-L2-started)
	 * OpenThread instance. Without this, Thread still runs — Zephyr starts
	 * it on its own — but every CHIP feature needing the OT instance fails
	 * quietly; the SRP hostname build then aborts DNS-SD advertising with
	 * NOT_FOUND and the node is never discoverable for on-network
	 * commissioning. */
	MCUHOME_STAGE("InitThreadStack", chip::DeviceLayer::ThreadStackMgr().InitThreadStack());
	MCUHOME_STAGE(
		"SetThreadDeviceType",
		chip::DeviceLayer::ConnectivityMgr().SetThreadDeviceType(
			IS_ENABLED(CONFIG_MCUHOME_MATTER_THREAD_ROUTER)
				? chip::DeviceLayer::ConnectivityManager::kThreadDeviceType_Router
				: chip::DeviceLayer::ConnectivityManager::
					  kThreadDeviceType_MinimalEndDevice));
#endif /* CONFIG_NET_L2_OPENTHREAD */

	static chip::CommonCaseDeviceServerInitParams initParams;

	MCUHOME_STAGE("InitStaticResources",
		      initParams.InitializeStaticResourcesBeforeServerInit());

	/* The prototype assigned this without checking. A null provider does
	 * not fail here — it fails later, deep inside the interaction model,
	 * as an unexplained read failure. */
	initParams.dataModelProvider =
		chip::app::CodegenDataModelProviderInstance(initParams.persistentStorageDelegate);
	if (initParams.dataModelProvider == nullptr) {
		LOG_ERR("stage DataModelProvider failed: no codegen provider instance");
		mcuhome_matter_stage("DataModelProvider", -ENODEV);
		return -ENODEV;
	}
	mcuhome_matter_stage("DataModelProvider", 0);

#ifdef CONFIG_MCUHOME_MATTER_TEST_DAC
	/* ADR 0012 path A: CHIP's example (test-certificate) attestation
	 * credentials. MCUHome's own attestation root is a v1.0 deliverable.
	 * The #error above guards the "no provider at all" case, so this
	 * #ifdef is the seam an alternative provider slots into rather than a
	 * branch that can silently leave the node without credentials. */
	chip::Credentials::SetDeviceAttestationCredentialsProvider(
		chip::Credentials::Examples::GetExampleDACProvider());
#endif

	MCUHOME_STAGE("ServerInit", chip::Server::GetInstance().Init(initParams));

	/* Endpoints come from the generated tables, translated to ember
	 * metadata by endpoint_registry.cpp. */
	MCUHOME_STAGE_ERRNO("RegisterEndpoints", mcuhome_matter_registry_register(node));

	MCUHOME_STAGE("StartEventLoop", chip::DeviceLayer::PlatformMgr().StartEventLoopTask());

	mcuhome_matter_dnssd_retry_arm();

	LOG_INF("Matter up: %u endpoint(s) registered", (unsigned int)node->endpoint_count);
	return 0;
}

void mcuhome_matter_attr_changed(uint16_t endpoint_id, uint32_t cluster_id, uint32_t attr_id)
{
	mcuhome_matter_registry_report(endpoint_id, cluster_id, attr_id);
}
