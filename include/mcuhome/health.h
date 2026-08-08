/*
 * SPDX-FileCopyrightText: 2026 The MCUHome Contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * MCUHome health foundation (ADR 0015, health amendment).
 *
 * Three mechanisms that only make sense together, and that an updatable
 * device cannot go without:
 *
 * 1. **A fatal error reboots.** Vanilla Zephyr halts (kernel/fatal.c's
 *    weak k_sys_fatal_error_handler() calls arch_system_halt()). A device
 *    built into a wall cannot be power-cycled by hand, and only a reboot
 *    lets MCUboot's revert machinery act at all.
 * 2. **A hardware watchdog resets a node whose loops stopped**, and it is
 *    fed from evidence rather than from a timer: every loop that has to
 *    keep running registers a liveness slot here and checks in; the feeder
 *    only feeds the watchdog while every slot is fresh.
 * 3. **A freshly swapped-in image confirms itself** to MCUboot once it has
 *    been healthy for a while. Until it does, the next boot swaps the
 *    previous image back — which is what makes a bad over-the-air update
 *    recoverable without a cable.
 * 4. **A crash leaves a breadcrumb behind.** Because (1) reboots, the
 *    fault dump is gone by the time anybody looks; a small record in
 *    reset-surviving RAM is logged on the next boot instead, and counted.
 *
 * All four are Kconfig-gated (lib/health/Kconfig) and all four default
 * to on for an application image.
 */

#ifndef MCUHOME_HEALTH_H_
#define MCUHOME_HEALTH_H_

#include <errno.h>
#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief Handle of one registered liveness slot.
 *
 * Opaque and non-negative on success. Negative values are errno codes
 * from mcuhome_health_liveness_register() and are safe to pass to
 * mcuhome_health_alive(), which ignores them — a caller that could not
 * register does not have to grow an error path for it.
 */
typedef int mcuhome_health_liveness_t;

/* Every function below has a do-nothing inline form for builds where its
 * Kconfig symbol is off, so that a caller never needs an #ifdef of its
 * own — a component that registers a liveness slot should read the same
 * whether or not this particular image has a watchdog. */
#if defined(CONFIG_MCUHOME_WATCHDOG)

/**
 * @brief Register a loop that has to keep running.
 *
 * The watchdog feeder refuses to feed while any registered slot has been
 * silent for longer than @p max_silence_ms, so the hardware watchdog
 * expires and the SoC resets. That is the whole point: a watchdog fed by
 * a timer proves that the timer runs, which is never the thing that
 * broke.
 *
 * Register before the loop starts and check in from inside it. A slot
 * starts out fresh, so a loop that registers and then never runs is
 * detected @p max_silence_ms later, not immediately.
 *
 * @param name         Short, stable identifier for the log line that
 *                     names the loop that went quiet. Must stay valid
 *                     forever (a string literal).
 * @param max_silence_ms How long this loop may go without checking in.
 *                     Give it several times its normal period: a missed
 *                     deadline reboots the device.
 *
 * @retval >=0 The slot handle.
 * @retval -ENOSPC No slot left (CONFIG_MCUHOME_WATCHDOG_LIVENESS_SLOTS).
 * @retval -EINVAL @p max_silence_ms is 0.
 * @retval -ENOTSUP The watchdog is not compiled in.
 */
mcuhome_health_liveness_t mcuhome_health_liveness_register(const char *name,
							   uint32_t max_silence_ms);

/**
 * @brief Check in on a registered liveness slot.
 *
 * Cheap, lock-free and safe from any context, including an ISR: it is a
 * single 64-bit uptime store. Ignores a negative @p slot.
 */
void mcuhome_health_alive(mcuhome_health_liveness_t slot);

#else /* !CONFIG_MCUHOME_WATCHDOG */

static inline mcuhome_health_liveness_t mcuhome_health_liveness_register(const char *name,
									 uint32_t max_silence_ms)
{
	(void)name;
	(void)max_silence_ms;
	return -ENOTSUP;
}

static inline void mcuhome_health_alive(mcuhome_health_liveness_t slot)
{
	(void)slot;
}

#endif /* CONFIG_MCUHOME_WATCHDOG */

#if defined(CONFIG_MCUHOME_CRASH_BREADCRUMB)

/**
 * @brief How many fatal errors this device has had since it last lost power.
 *
 * Counted in the same reset-surviving record that carries the crash
 * breadcrumb, so it is a count of *crashes*, not of reboots: a clean
 * restart, an over-the-air update or a watchdog reset does not raise it.
 * Zero on a device that has not crashed since power-on — and also zero
 * whenever the record did not survive intact, because a fault count that
 * makes numbers up would be worse than none.
 */
uint32_t mcuhome_health_fault_count(void);

#else /* !CONFIG_MCUHOME_CRASH_BREADCRUMB */

static inline uint32_t mcuhome_health_fault_count(void)
{
	return 0U;
}

#endif /* CONFIG_MCUHOME_CRASH_BREADCRUMB */

#if defined(CONFIG_MCUHOME_IMAGE_CONFIRM)

/**
 * @brief Report that the device reached its healthy operating point.
 *
 * Arms the image-confirmation timer: CONFIG_MCUHOME_IMAGE_CONFIRM_DELAY_S
 * later, an image that is running as MCUboot's *test* image confirms
 * itself and the swap becomes permanent. An image that crashes, hangs or
 * gets reset before that never confirms, and MCUboot swaps the previous
 * one back on the next boot.
 *
 * Called by the framework at the end of Matter bring-up. Idempotent —
 * repeated calls do not restart the delay, so a stack that re-runs a
 * stage cannot push confirmation out forever.
 */
void mcuhome_health_operational(void);

/**
 * @brief Whether the running image still has to confirm itself.
 *
 * True only while MCUboot is running this image as a test image and
 * nothing has confirmed it yet. False on a normal boot of a confirmed
 * image, and false once mcuhome_health_operational()'s timer has fired.
 */
bool mcuhome_health_image_pending(void);

#else /* !CONFIG_MCUHOME_IMAGE_CONFIRM */

static inline void mcuhome_health_operational(void)
{
}

static inline bool mcuhome_health_image_pending(void)
{
	return false;
}

#endif /* CONFIG_MCUHOME_IMAGE_CONFIRM */

#ifdef __cplusplus
}
#endif

#endif /* MCUHOME_HEALTH_H_ */
