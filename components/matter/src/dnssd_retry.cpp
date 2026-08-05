/*
 * SPDX-FileCopyrightText: 2026 The MCUHome Contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * Periodic DNS-SD advertisement retry — a framework service, not an
 * application concern.
 *
 * Upstream gap (candidate C7): on the OpenThread platform nothing ever
 * posts kDnssdInitialized / kDnssdRestartNeeded, so a commissionable-node
 * advertisement that fails inside Server::Init() is never retried and the
 * node stays undiscoverable until it is power-cycled at exactly the right
 * moment.
 *
 * Observed live on 2026-08-05 during E2E commissioning: the advertisement
 * at ServerInit fails because the SRP client has not started yet (normal
 * with a pre-provisioned Thread dataset — the SRP server is not in the
 * network data at that point), and the node recovers on one of the retries
 * below once SRP is up. Re-running the DNS-SD server start is idempotent.
 */

#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>

#include <app/server/Dnssd.h>
#include <platform/CHIPDeviceLayer.h>

#include "matter_internal.h"

LOG_MODULE_DECLARE(MCUHOME_MATTER_LOG_MODULE, CONFIG_LOG_DEFAULT_LEVEL);

namespace
{

constexpr int kRetryCount = CONFIG_MCUHOME_MATTER_DNSSD_RETRY_COUNT;
constexpr int kRetryIntervalSecs = CONFIG_MCUHOME_MATTER_DNSSD_RETRY_INTERVAL_S;

int gRetriesLeft;
bool gArmed;

void RestartDnssd(intptr_t /* unused */)
{
	chip::app::DnssdServer::Instance().StartServer();
}

void RetryHandler(struct k_work *work)
{
	struct k_work_delayable *dwork = k_work_delayable_from_work(work);

	/* Hand the actual call to the CHIP event loop: DnssdServer touches
	 * stack state and this handler runs on the system workqueue. */
	CHIP_ERROR err = chip::DeviceLayer::PlatformMgr().ScheduleWork(RestartDnssd, 0);

	if (err != CHIP_NO_ERROR) {
		LOG_WRN("DNS-SD retry not scheduled: %" CHIP_ERROR_FORMAT, err.Format());
	}

	if (--gRetriesLeft > 0) {
		k_work_reschedule(dwork, K_SECONDS(kRetryIntervalSecs));
	} else {
		LOG_INF("DNS-SD retry budget exhausted (%d attempts)", kRetryCount);
	}
}

K_WORK_DELAYABLE_DEFINE(gRetryWork, RetryHandler);

} /* namespace */

void mcuhome_matter_dnssd_retry_arm(void)
{
	if (kRetryCount <= 0) {
		return;
	}
	if (gArmed) {
		/* Idempotent: re-arming must not stack timers or hand out a
		 * fresh retry budget. */
		return;
	}

	gArmed = true;
	gRetriesLeft = kRetryCount;

	LOG_INF("DNS-SD advertisement retry armed: %d attempts, %d s apart (upstream gap C7)",
		kRetryCount, kRetryIntervalSecs);

	/* First attempt immediately, matching the timing proven in the E2E
	 * prototype (its main loop fired one StartServer() right after the
	 * event loop came up, then one every interval). */
	k_work_reschedule(&gRetryWork, K_NO_WAIT);
}
