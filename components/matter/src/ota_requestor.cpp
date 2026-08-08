/*
 * SPDX-FileCopyrightText: 2026 The MCUHome Contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * Matter OTA Requestor wiring — the application half of ADR 0015
 * decision 5, item 3.
 *
 * Four upstream objects and one MCUHome one, connected once at bring-up:
 *
 *   DefaultOTARequestor         the cluster 0x002A server: takes
 *                               AnnounceOTAProvider, drives the query and
 *                               apply state machine.
 *   DefaultOTARequestorStorage  persists the DefaultOTAProviders list and
 *                               the in-flight update state in the same
 *                               settings/NVS partition as the fabric.
 *   DefaultOTARequestorDriver   the policy: when to query, which provider
 *                               to try next, what to do with a busy one.
 *   BDXDownloader               the transfer.
 *   mcuhome::OtaImageProcessor  where the bytes go (ota_image_processor.h).
 *
 * WHY THE POLICY IS UPSTREAM'S AND NOT OURS. The driver interface
 * (OTARequestorDriver.h) has fifteen virtuals, and the parts that are not
 * trivial — provider-list traversal with a busy-retry budget, the periodic
 * query timer, the download watchdog, retry-on-invalid-session — are
 * exactly the parts a from-scratch implementation would get subtly wrong
 * and then have to maintain against a moving specification. MCUHome's
 * policy is therefore expressed as configuration of the upstream driver
 * (the query interval below) plus one deliberate override of its
 * behaviour, in the image processor: confirmation is NOT immediate,
 * it waits for healthy uptime (ADR 0015's health amendment). That is the
 * one place where MCUHome disagrees with upstream, and it is one function
 * rather than a fork of the driver.
 *
 * Enabling CONFIG_CHIP_OTA_REQUESTOR alone does nothing measurable — the
 * phase-3 measurement found the =y and =n images byte-identical, because
 * the framework ZAP carried no cluster 0x002A and --gc-sections dropped
 * everything CHIP had compiled. This file is what references it.
 */

#include <zephyr/logging/log.h>

#include <app/clusters/ota-requestor/BDXDownloader.h>
#include <app/clusters/ota-requestor/DefaultOTARequestor.h>
#include <app/clusters/ota-requestor/DefaultOTARequestorDriver.h>
#include <app/clusters/ota-requestor/DefaultOTARequestorStorage.h>
#include <app/server/Server.h>
#include <platform/CHIPDeviceLayer.h>

#include "matter_internal.h"
#include "ota_image_processor.h"

LOG_MODULE_DECLARE(MCUHOME_MATTER_LOG_MODULE, CONFIG_LOG_DEFAULT_LEVEL);

namespace
{

chip::DefaultOTARequestorStorage sStorage;
chip::DeviceLayer::DefaultOTARequestorDriver sDriver;
chip::BDXDownloader sDownloader;
chip::DefaultOTARequestor sRequestor;

} // namespace

int mcuhome_matter_ota_init(void)
{
	if (chip::GetRequestorInstance() != nullptr) {
		/* Already wired. Idempotent because bring-up stages are
		 * retried (CONFIG_MCUHOME_MATTER_INIT_RETRY_FOREVER) and
		 * registering the requestor twice would leave CHIP holding a
		 * pointer to a half-initialized object. */
		return 0;
	}

	mcuhome::OtaImageProcessor &processor = mcuhome::OtaImageProcessor::Instance();

	processor.SetDownloader(&sDownloader);
	sDownloader.SetImageProcessorDelegate(&processor);
	sStorage.Init(chip::Server::GetInstance().GetPersistentStorage());
	sRequestor.Init(chip::Server::GetInstance(), sStorage, sDriver, sDownloader);
	chip::SetRequestorInstance(&sRequestor);

	/* How often the node asks the providers on its DefaultOTAProviders
	 * list whether there is something newer. Upstream's default is 24 h;
	 * this is a Kconfig because "how eager is your update check" is a
	 * product decision and not a framework constant. Note that a
	 * controller can also push an update at any time with
	 * AnnounceOTAProvider, which is the path Home Assistant uses — the
	 * periodic query is the fallback for a device nobody announced to. */
	sDriver.SetPeriodicQueryTimeout(CONFIG_MCUHOME_MATTER_OTA_QUERY_INTERVAL_S);

	/* Init() last, and after SetRequestorInstance(): the driver asks the
	 * image processor whether this is the first run of a new image, and
	 * that question is answered from the requestor's own persisted
	 * state (OtaImageProcessor::IsFirstImageRun). */
	sDriver.Init(&sRequestor, &processor);

	LOG_INF("OTA requestor ready, querying every %u s",
		CONFIG_MCUHOME_MATTER_OTA_QUERY_INTERVAL_S);
	return 0;
}
