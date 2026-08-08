/*
 * SPDX-FileCopyrightText: 2026 The MCUHome Contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * Fault reboot and the evidence-fed hardware watchdog (<mcuhome/health.h>,
 * ADR 0015 health amendment).
 *
 * WHAT THE WATCHDOG IS FED FROM, AND WHY IT MATTERS
 *
 * The easy version of this file is a k_timer that calls wdt_feed(). It is
 * also worthless: a kernel timer keeps firing from the system clock ISR
 * long after every thread in the system has deadlocked, so what it proves
 * is that the SoC still has a clock. The failures that actually happen on
 * a Matter node — the CHIP event loop blocked on a lock, a sensor poller
 * stuck in an I2C transfer that never completes, a workqueue drowned by a
 * handler that never returns — all leave that timer running.
 *
 * So the feed is conditional on evidence:
 *
 *  - The feeder itself is a k_work_delayable on the SYSTEM WORKQUEUE.
 *    Running at all is the proof that the system workqueue still
 *    schedules, which is why that loop needs no slot of its own.
 *  - Every other loop that has to keep running registers a slot
 *    (mcuhome_health_liveness_register) and stamps it from inside itself.
 *    The framework registers exactly one: the CHIP event loop, from a
 *    repeating CHIP timer. The sensor poller deliberately does NOT get a
 *    slot — components/sensor is written so that a missing, unready or
 *    failing sensor invalidates its attribute and nothing else, and
 *    rebooting a commissioned node because one I2C part stopped
 *    answering would be a worse outcome than the null readings it
 *    already reports.
 *  - The feeder feeds only while every slot is fresh. One stale slot and
 *    it stops feeding; CONFIG_MCUHOME_WATCHDOG_TIMEOUT_S later the SoC
 *    resets.
 *
 * Consequence worth stating: the watchdog timeout is the backstop for the
 * feeder, and the per-slot silence budget is the real detector. Sizing the
 * budgets is therefore each loop's own decision, made where the loop's
 * period is known.
 */

#include <errno.h>
#include <stdbool.h>
#include <stdint.h>

#include <zephyr/devicetree.h>
#include <zephyr/fatal.h>
#include <zephyr/init.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/logging/log_ctrl.h>
#include <zephyr/sys/reboot.h>
#include <zephyr/toolchain.h>

#ifdef CONFIG_MCUHOME_WATCHDOG
#include <zephyr/drivers/watchdog.h>
#endif

#include <mcuhome/health.h>

LOG_MODULE_REGISTER(mcuhome_health, CONFIG_LOG_DEFAULT_LEVEL);

/* --- Fatal errors reboot, they do not halt ------------------------------ */

#ifdef CONFIG_MCUHOME_RESET_ON_FATAL_ERROR
/* Overrides the weak definition in Zephyr's kernel/fatal.c, which logs
 * "Halting system" and calls arch_system_halt(). A halted device is
 * unreachable and — the reason this belongs in the update block — never
 * reaches the boot in which MCUboot would revert an unconfirmed image.
 *
 * LOG_PANIC() first: it switches every backend to synchronous mode and
 * drains what is queued, so the fault report reaches the console before
 * the reset takes the console with it. Zephyr's own handler does the same
 * for the same reason.
 */
FUNC_NORETURN void k_sys_fatal_error_handler(unsigned int reason, const struct arch_esf *esf)
{
	ARG_UNUSED(esf);

	LOG_PANIC();
	LOG_ERR("fatal error %u — rebooting", reason);
	sys_reboot(SYS_REBOOT_COLD);

	CODE_UNREACHABLE;
}
#endif /* CONFIG_MCUHOME_RESET_ON_FATAL_ERROR */

/* --- The watchdog ------------------------------------------------------- */

#ifdef CONFIG_MCUHOME_WATCHDOG

#if !DT_HAS_ALIAS(watchdog0)
#error "CONFIG_MCUHOME_WATCHDOG needs a watchdog0 devicetree alias, and this board has none. " \
	"The alias is board wiring: add it to the board's overlay (mcuhome/registry.py's BoardDef " \
	"for a generated application, samples/*/boards/ for a sample):\n" \
	"    / { aliases { watchdog0 = &wdt0; }; };\n" \
	"ADR 0015's health amendment makes the watchdog mandatory for every application image, so " \
	"this is a build error rather than a silently disabled feature."
#endif

#define WATCHDOG_NODE       DT_ALIAS(watchdog0)
#define WATCHDOG_TIMEOUT_MS (CONFIG_MCUHOME_WATCHDOG_TIMEOUT_S * 1000U)
#define FEED_INTERVAL_MS    (CONFIG_MCUHOME_WATCHDOG_FEED_INTERVAL_S * 1000U)

/* A late work item must never be able to reset the device on its own: the
 * feeder has to get at least two more chances after missing one. */
BUILD_ASSERT(FEED_INTERVAL_MS * 3U <= WATCHDOG_TIMEOUT_MS,
	     "CONFIG_MCUHOME_WATCHDOG_FEED_INTERVAL_S must be at most a third of "
	     "CONFIG_MCUHOME_WATCHDOG_TIMEOUT_S — otherwise one late work item resets the SoC");

/** One registered loop. Written by its owner, read by the feeder. */
struct liveness_slot {
	const char *name;
	uint32_t max_silence_ms;
	/* Uptime of the last check-in. 64-bit and written atomically enough
	 * for this purpose on every target Zephyr supports: the feeder only
	 * ever compares it against now, so reading a half-updated value can
	 * at worst delay a reset by one feed interval — it can never cause
	 * a spurious one, because both halves only ever move forward. */
	int64_t last_seen;
};

static const struct device *const watchdog = DEVICE_DT_GET(WATCHDOG_NODE);
static struct liveness_slot slots[CONFIG_MCUHOME_WATCHDOG_LIVENESS_SLOTS];
static uint8_t slot_count;
static int watchdog_channel = -1;

static void feed_work_handler(struct k_work *work);
static K_WORK_DELAYABLE_DEFINE(feed_work, feed_work_handler);

mcuhome_health_liveness_t mcuhome_health_liveness_register(const char *name,
							   uint32_t max_silence_ms)
{
	unsigned int key;
	uint8_t index;

	if (max_silence_ms == 0U) {
		return -EINVAL;
	}

	/* Registration races the feeder, which walks the same array from the
	 * system workqueue. The critical section is a handful of stores. */
	key = irq_lock();
	if (slot_count == ARRAY_SIZE(slots)) {
		irq_unlock(key);
		LOG_ERR("no liveness slot left for %s — raise "
			"CONFIG_MCUHOME_WATCHDOG_LIVENESS_SLOTS",
			name);
		return -ENOSPC;
	}
	index = slot_count;
	slots[index].name = name;
	slots[index].max_silence_ms = max_silence_ms;
	/* Fresh on registration: a loop gets its full budget to produce its
	 * first check-in, instead of having to race the next feed. */
	slots[index].last_seen = k_uptime_get();
	slot_count = index + 1U;
	irq_unlock(key);

	LOG_DBG("liveness slot %u: %s, %u ms", index, name, max_silence_ms);
	return (mcuhome_health_liveness_t)index;
}

void mcuhome_health_alive(mcuhome_health_liveness_t slot)
{
	if (slot < 0 || (uint8_t)slot >= slot_count) {
		return;
	}
	slots[slot].last_seen = k_uptime_get();
}

/** The stale slot, or NULL when every registered loop is fresh. */
static const struct liveness_slot *stale_slot(int64_t now)
{
	for (uint8_t index = 0; index < slot_count; index++) {
		const struct liveness_slot *slot = &slots[index];

		if ((now - slot->last_seen) > (int64_t)slot->max_silence_ms) {
			return slot;
		}
	}
	return NULL;
}

static void feed_work_handler(struct k_work *work)
{
	const struct liveness_slot *stale = stale_slot(k_uptime_get());

	ARG_UNUSED(work);

	if (stale == NULL) {
		(void)wdt_feed(watchdog, watchdog_channel);
	} else {
		/* Not an error yet — the loop may still come back before the
		 * watchdog expires, and saying so is the only diagnostic
		 * anybody will have afterwards. */
		LOG_WRN("not feeding the watchdog: %s has not checked in", stale->name);
	}

	/* Rescheduled unconditionally, including after a skipped feed: the
	 * device only resets if the loop stays quiet, not because the feeder
	 * gave up on it. */
	(void)k_work_reschedule(&feed_work, K_MSEC(FEED_INTERVAL_MS));
}

static int watchdog_start(void)
{
	static const struct wdt_timeout_cfg timeout = {
		/* No window: nRF hardware rejects a non-zero minimum, and a
		 * "too early" bound would only turn a healthy early feed into
		 * a reset. */
		.window = {.min = 0U, .max = WATCHDOG_TIMEOUT_MS},
		/* No interrupt callback on purpose. The nRF watchdog gives a
		 * ~61 us grace period after the interrupt, which is not
		 * enough to do anything useful and is plenty to hang in. Let
		 * the hardware reset the SoC. */
		.callback = NULL,
		.flags = WDT_FLAG_RESET_SOC,
	};
	int rc;

	if (!device_is_ready(watchdog)) {
		LOG_ERR("watchdog %s is not ready — running without one", watchdog->name);
		return -ENODEV;
	}

	rc = wdt_install_timeout(watchdog, &timeout);
	if (rc < 0) {
		LOG_ERR("wdt_install_timeout failed: %d", rc);
		return rc;
	}
	watchdog_channel = rc;

	/* WDT_OPT_PAUSE_HALTED_BY_DBG: a breakpoint must not reset the board
	 * under the debugger. It stays running in sleep — a Matter node
	 * spends most of its life there, and a watchdog that pauses then is
	 * a watchdog that never fires. */
	rc = wdt_setup(watchdog, WDT_OPT_PAUSE_HALTED_BY_DBG);
	if (rc < 0) {
		LOG_ERR("wdt_setup failed: %d", rc);
		watchdog_channel = -1;
		return rc;
	}

	(void)k_work_reschedule(&feed_work, K_MSEC(FEED_INTERVAL_MS));
	LOG_INF("watchdog armed: %u s timeout, fed every %u s", CONFIG_MCUHOME_WATCHDOG_TIMEOUT_S,
		CONFIG_MCUHOME_WATCHDOG_FEED_INTERVAL_S);
	return 0;
}

/* APPLICATION level, so that every driver the watchdog device depends on
 * is initialized and the system workqueue exists. Deliberately before
 * main(): bring-up itself is a place a node can hang, and the timeout is
 * generous enough to cover a slow one.
 *
 * A failure here is logged, not propagated: refusing to boot because the
 * watchdog would not arm turns a degraded node into a dead one. */
static int mcuhome_health_watchdog_init(void)
{
	(void)watchdog_start();
	return 0;
}

SYS_INIT(mcuhome_health_watchdog_init, APPLICATION, CONFIG_APPLICATION_INIT_PRIORITY);

#endif /* CONFIG_MCUHOME_WATCHDOG */
