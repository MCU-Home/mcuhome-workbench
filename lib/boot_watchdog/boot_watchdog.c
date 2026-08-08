/*
 * SPDX-FileCopyrightText: 2026 The MCUHome Contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * The bootloader's watchdog (CONFIG_MCUHOME_BOOT_WATCHDOG, MCUboot image
 * only — see the Kconfig help for why the bootloader cannot go without
 * one).
 *
 * Two entry points, one arming:
 *
 *  - A PRE_KERNEL_2 SYS_INIT arms the watchdog as early as a Zephyr
 *    driver can be used — the watchdog device itself initializes at
 *    PRE_KERNEL_1. MCUboot's own arming happens in main(), and the
 *    stretch between kernel init and main() is exactly where a fault
 *    with no watchdog was observed to strand a device in the fault
 *    handler (bench, 2026-08-08). What remains unguarded is only the
 *    pre-kernel window (SystemInit, PRE_KERNEL_1 driver init).
 *
 *  - mcuboot_watchdog_setup() — the STRONG override of the weak default
 *    in MCUboot's boot/zephyr/watchdog.c, which main() calls. By then
 *    the watchdog is already armed, so this reduces to a no-op (with a
 *    fallback attempt in case the early arming failed).
 *
 * THE OPTIONS THIS PASSES DECIDE FOR THE WHOLE BOOT CHAIN. On nRF
 * hardware the watchdog configuration is write-locked from the moment
 * the watchdog starts, so whatever is set here is what the application
 * runs under, whatever lib/health then asks for. The default is options
 * 0 — the watchdog keeps counting in sleep AND while the core is halted
 * by a debugger — because a watchdog that any stale debug connection
 * disables is not a backstop, and a bench node was found in exactly that
 * state on 2026-08-08: an hour and a half in a fault handler with an
 * armed 60 s watchdog that could not fire.
 * CONFIG_MCUHOME_BOOT_WATCHDOG_PAUSE_ON_DEBUG is the deliberate bench
 * override, and it has to be set here, not only in the application.
 *
 * The timeout is MCUboot's CONFIG_BOOT_WATCHDOG_TIMEOUT_MS, which the
 * generated sysbuild/mcuboot.conf pins to the application's
 * CONFIG_MCUHOME_WATCHDOG_TIMEOUT_S — one number for the whole boot
 * chain, because the hardware allows only one: the nRF watchdog cannot
 * be reconfigured after it starts, so whatever the bootloader arms is
 * what the application runs under. MCUboot feeds it through
 * MCUBOOT_WATCHDOG_FEED() in its swap loops and its serial-recovery
 * loop; the application's evidence-fed feeder (lib/health) takes over
 * from there.
 */

#include <zephyr/device.h>
#include <zephyr/devicetree.h>
#include <zephyr/drivers/watchdog.h>
#include <zephyr/init.h>
#include <zephyr/logging/log.h>

/* NOT CONFIG_LOG_DEFAULT_LEVEL. MCUboot's own boot/zephyr/prj.conf pins
 * that symbol to 0 and keeps its logging under MCUBOOT_LOG_LEVEL instead,
 * so a module registered at the default level compiles every line in this
 * file away — including the one that would say the bootloader's watchdog
 * did not arm. Silence there is precisely the failure this file exists to
 * prevent, and the debug-output directive (AGENTS.md) does not allow it.
 * The fallback is for a build outside an MCUboot image, which nothing in
 * this repository does. */
#ifdef CONFIG_MCUBOOT_LOG_LEVEL
#define BOOT_WATCHDOG_LOG_LEVEL CONFIG_MCUBOOT_LOG_LEVEL
#else
#define BOOT_WATCHDOG_LOG_LEVEL CONFIG_LOG_DEFAULT_LEVEL
#endif

LOG_MODULE_REGISTER(mcuhome_boot_wdt, BOOT_WATCHDOG_LOG_LEVEL);

#if !DT_HAS_ALIAS(watchdog0)
#error "CONFIG_MCUHOME_BOOT_WATCHDOG needs a watchdog0 devicetree alias, and this build has " \
	"none. The alias is board wiring (the same one lib/health needs in the application " \
	"image); a bootloader without a watchdog is a bootloader that can strand the device " \
	"in a fault handler mid-swap, so this is a build error rather than a silently " \
	"disabled feature."
#endif

/* MCUboot's symbol (boot/zephyr/Kconfig, "Watchdog configuration"). The
 * fallback only exists so that this file states its own requirement when
 * compiled outside an MCUboot image, which nothing in this repository
 * does. */
#ifdef CONFIG_BOOT_WATCHDOG_TIMEOUT_MS
#define BOOT_WATCHDOG_TIMEOUT_MS CONFIG_BOOT_WATCHDOG_TIMEOUT_MS
#else
#define BOOT_WATCHDOG_TIMEOUT_MS 60000
#endif

static const struct device *const watchdog = DEVICE_DT_GET(DT_ALIAS(watchdog0));
static bool armed;

static int boot_watchdog_arm(void)
{
	static const struct wdt_timeout_cfg timeout = {
		/* No window, no callback, reset the SoC: identical to the
		 * application's configuration in lib/health/health.c, and
		 * for the same reasons documented there. */
		.window = {.min = 0U, .max = BOOT_WATCHDOG_TIMEOUT_MS},
		.callback = NULL,
		.flags = WDT_FLAG_RESET_SOC,
	};
	int rc;

	if (!device_is_ready(watchdog)) {
		LOG_ERR("boot watchdog %s is not ready — bootloader runs without one",
			watchdog->name);
		return 0;
	}

	rc = wdt_install_timeout(watchdog, &timeout);
	if (rc < 0) {
		LOG_ERR("boot watchdog install failed: %d", rc);
		return 0;
	}

	rc = wdt_setup(watchdog, IS_ENABLED(CONFIG_MCUHOME_BOOT_WATCHDOG_PAUSE_ON_DEBUG)
					 ? WDT_OPT_PAUSE_HALTED_BY_DBG
					 : 0);
	if (rc < 0) {
		LOG_ERR("boot watchdog setup failed: %d", rc);
		return 0;
	}

	armed = true;
	LOG_INF("boot watchdog armed: %u ms, %s while halted by a debugger",
		(unsigned int)BOOT_WATCHDOG_TIMEOUT_MS,
		IS_ENABLED(CONFIG_MCUHOME_BOOT_WATCHDOG_PAUSE_ON_DEBUG) ? "paused" : "running");
	return 0;
}

/* A failure is logged, never propagated: refusing to boot because the
 * watchdog would not arm turns a degraded bootloader into a dead one —
 * the same policy as the application's watchdog. */
SYS_INIT(boot_watchdog_arm, PRE_KERNEL_2, 0);

/* Strong override of the weak definition in MCUboot's
 * boot/zephyr/watchdog.c (see the file comment). Called by MCUboot's
 * main(); by then the SYS_INIT above has normally done the work. */
void mcuboot_watchdog_setup(void)
{
	if (!armed) {
		(void)boot_watchdog_arm();
	}
}
