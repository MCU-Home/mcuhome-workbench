/*
 * SPDX-FileCopyrightText: 2026 The MCUHome Contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * Framework-internal interfaces of components/matter. Not installed and
 * not part of the public API: application and generated code use
 * <mcuhome/matter.h> and <mcuhome/matter_tables.h> only.
 */

#ifndef MCUHOME_COMPONENTS_MATTER_INTERNAL_H_
#define MCUHOME_COMPONENTS_MATTER_INTERNAL_H_

#include <mcuhome/matter_tables.h>

/** Log module shared by all framework sources. */
#define MCUHOME_MATTER_LOG_MODULE mcuhome_matter

/**
 * @brief Validate the generated tables and register every endpoint.
 *
 * Translates the plain-C tables into ember metadata in statically sized
 * pools and calls emberAfSetDynamicEndpoint() for each endpoint. Takes the
 * CHIP stack lock internally around the ember calls.
 *
 * Must be called after Server::Init() and before the CHIP event loop
 * starts. Keeps @p node for the lifetime of the process — the external
 * attribute callbacks dispatch through it.
 *
 * @return 0 on success, or the negative errno mcuhome_matter_start()
 *         documents (-EINVAL / -EEXIST / -ENOSPC / -EIO).
 */
int mcuhome_matter_registry_register(const struct mcuhome_matter_node *node);

/**
 * @brief Queue a Matter attribute change report on the CHIP event loop.
 *
 * Implementation behind mcuhome_matter_attr_changed(); lives with the
 * registry because it shares its view of the registered tables.
 */
void mcuhome_matter_registry_report(uint16_t endpoint_id, uint32_t cluster_id, uint32_t attr_id);

/**
 * @brief Start the periodic DNS-SD advertisement retry (upstream gap C7).
 *
 * Idempotent: repeated calls do not stack up timers or reset the retry
 * budget. No-op when CONFIG_MCUHOME_MATTER_DNSSD_RETRY_COUNT is 0.
 */
void mcuhome_matter_dnssd_retry_arm(void);

#endif /* MCUHOME_COMPONENTS_MATTER_INTERNAL_H_ */
