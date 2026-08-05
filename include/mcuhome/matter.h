/*
 * SPDX-FileCopyrightText: 2026 The MCUHome Contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * MCUHome Matter runtime API (ADR 0014).
 *
 * The C-callable surface of components/matter. Application code — including
 * everything the builder generates — talks to Matter exclusively through
 * this header plus <mcuhome/matter_tables.h>. No CHIP header, no ember
 * macro and no CHIP lock ever appears outside components/matter.
 */

#ifndef MCUHOME_MATTER_H_
#define MCUHOME_MATTER_H_

#include <stdint.h>

#include <mcuhome/matter_tables.h>

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief Bring the Matter stack up and register the generated endpoints.
 *
 * Runs the full bring-up sequence — CHIP memory and stack init, Thread
 * attach and role, server init with the codegen data-model provider,
 * attestation credentials, registration of every endpoint in @p node, the
 * CHIP event loop and the DNS-SD retry service. Blocks until the stack is
 * up; the caller's thread is free afterwards.
 *
 * Every stage reports through mcuhome_matter_stage().
 *
 * @param node Generated device configuration. Must stay valid forever (it
 *             is `const` data in flash); the framework keeps the pointer.
 *
 * @retval 0 Matter is up and all endpoints are registered.
 * @retval -EINVAL @p node is NULL, its `tables_version` does not match
 *                 MCUHOME_MATTER_TABLES_VERSION, or a table entry is
 *                 malformed (bad endpoint ID, unknown attribute type, …).
 * @retval -EEXIST Two endpoints share an ID, or an ID collides with the
 *                 statically compiled root endpoint.
 * @retval -ENOSPC A CONFIG_MCUHOME_MATTER_MAX_* pool is too small for the
 *                 tables.
 * @retval -EIO A CHIP stage failed, or endpoint registration failed after
 *              validation passed (ember rejected a table it had no
 *              structural objection to — see below). The CHIP error code
 *              itself is logged and passed to mcuhome_matter_stage(); it is
 *              not encoded in this return value.
 *
 * With CONFIG_MCUHOME_MATTER_INIT_RETRY_FOREVER=y (the default) failing
 * CHIP bring-up stages (memory/stack/Thread/server init, …) are retried
 * indefinitely and never surface as a return from this function. Endpoint
 * registration is NOT one of those stages: it is attempted exactly once and
 * a failure there returns -EIO immediately, retry setting notwithstanding.
 * Retrying cannot help — a registration failure is ember rejecting the
 * translated table (for example CHIP_ERROR_ENDPOINT_EXISTS), which means
 * the table itself is wrong and will be wrong again next attempt. Table
 * validation errors (-EINVAL/-EEXIST/-ENOSPC) are likewise never retried,
 * for the same reason.
 */
int mcuhome_matter_start(const struct mcuhome_matter_node *node);

/**
 * @brief Report that an attribute's store cell changed.
 *
 * Safe to call from any application thread. The framework hands the
 * reporting call to the CHIP event loop internally, so callers never take
 * the CHIP stack lock — and never have to know it exists.
 *
 * (The E2E prototype called MatterReportingAttributeChangeCallback()
 * directly from its main loop. That worked because the loop happened to be
 * the only writer, but it is an unlocked cross-thread call into the stack
 * and not a pattern generated code may inherit.)
 *
 * Write the value into the attribute's struct mcuhome_attr_store first,
 * then call this. Reporting is asynchronous: on return the change is
 * queued, not yet on the wire.
 *
 * @param endpoint_id Endpoint the attribute belongs to.
 * @param cluster_id  Cluster the attribute belongs to.
 * @param attr_id     Attribute that changed.
 */
void mcuhome_matter_attr_changed(uint16_t endpoint_id, uint32_t cluster_id, uint32_t attr_id);

/**
 * @brief Bring-up progress hook.
 *
 * Called by mcuhome_matter_start() once per stage *outcome*, after the
 * stage ran:
 *
 * - @p err == 0: the stage completed successfully. Every stage reports
 *   success exactly once, in bring-up order.
 * - @p err != 0: the stage failed. @p err is the CHIP error code cast to
 *   int for CHIP stages, or the negative errno mcuhome_matter_start()
 *   would return for framework-side failures (table validation, pool
 *   sizing). With CONFIG_MCUHOME_MATTER_INIT_RETRY_FOREVER=y a failing
 *   CHIP stage reports once per retry attempt and is eventually followed
 *   by an @p err == 0 report for the same stage when it succeeds.
 *
 * @p name is a short, stable, framework-owned stage identifier (for
 * example "InitChipStack"); it is a string literal and stays valid.
 *
 * The default implementation logs. Applications may override it — it is a
 * weak symbol — for example to drive an LED pattern on a board without a
 * console. Overrides run on the calling thread of mcuhome_matter_start()
 * and may block; they must not call back into the Matter API.
 */
void mcuhome_matter_stage(const char *name, int err);

#ifdef __cplusplus
}
#endif

#endif /* MCUHOME_MATTER_H_ */
