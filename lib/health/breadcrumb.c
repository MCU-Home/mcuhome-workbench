/*
 * SPDX-FileCopyrightText: 2026 The MCUHome Contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * The crash breadcrumb (CONFIG_MCUHOME_CRASH_BREADCRUMB): everything
 * about it that touches hardware, linker sections and the log. The pure
 * logic — the record layout, its integrity word and the sequence rule —
 * is in breadcrumb_core.h, where the native_sim suite in
 * tests/health_breadcrumb can drive every branch of it on the host.
 *
 * WHY THIS EXISTS. A fatal error on an MCUHome node reboots
 * (CONFIG_MCUHOME_RESET_ON_FATAL_ERROR). That is correct and it is also
 * why nobody ever learns what happened: Zephyr's fault dump goes out
 * over a log transport that on a deployed device has no reader, the
 * device resets, and the evidence is gone with the RAM the log buffer
 * was in. What survives a soft reset on these targets is RAM that
 * nothing initializes — so the handler writes the few words that matter
 * there, and the next boot, which may well have a reader attached, says
 * them out loud.
 *
 * WHAT IT CAN AND CANNOT KNOW. PC and LR come from the exception stack
 * frame and are always real. The SCB fault registers are best-effort:
 * Zephyr's own fault path prints CFSR and then CLEARS the sticky bits
 * (arch/arm/core/cortex_m/fault.c) long before a fatal handler runs, so
 * CFSR usually reads back as zero here. BFAR is not cleared and often
 * still holds the faulting address, which is the single most useful
 * number in the record. Nothing is inferred: what the registers say is
 * what is stored.
 *
 * WHERE THE RECORD LIVES. `__noinit` — a linker section the startup code
 * does not zero. That gets it across NVIC_SystemReset, which is what
 * sys_reboot(SYS_REBOOT_COLD) does on these SoCs. It does NOT get it
 * across a power cycle, and it is not protected from MCUboot, which owns
 * the same RAM before the application does and may put its own stack
 * there. Both cases show up as a broken checksum and the record is
 * silently dropped. That asymmetry is deliberate: a lost crash report
 * costs a debugging session, an invented one costs trust in every report
 * after it.
 */

#include <stdbool.h>
#include <stdint.h>

#include <zephyr/arch/cpu.h>
#include <zephyr/init.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/toolchain.h>

#include <mcuhome/health.h>

#include "breadcrumb.h"
#include "breadcrumb_core.h"

LOG_MODULE_DECLARE(mcuhome_health, CONFIG_LOG_DEFAULT_LEVEL);

/* Not static: a linker-section variable that nothing else references is
 * exactly the shape a link-time optimizer likes to remove, and a symbol
 * in the map file is also how a bench session finds the record with a
 * debugger after a reset that never got to report it. */
__noinit struct mcuhome_crash_breadcrumb mcuhome_crash_breadcrumb;

/* The configurable fault registers exist on ARMv7-M and ARMv8-M mainline
 * only — the same guard Zephyr's own fault dump uses
 * (arch/arm/core/cortex_m/fault.c). Everywhere else the three words are
 * recorded as zero, which is what breadcrumb_core.h documents them as. */
#if defined(CONFIG_ARMV7_M_ARMV8_M_MAINLINE)
#include <cmsis_core.h>

static void read_fault_registers(uint32_t *cfsr, uint32_t *hfsr, uint32_t *bfar)
{
	*cfsr = SCB->CFSR;
	*hfsr = SCB->HFSR;
	/* Only meaningful while BFARVALID is set — but Zephyr's handler has
	 * usually cleared CFSR by now, taking BFARVALID with it, while BFAR
	 * itself still holds the address. Store it and say so in the log
	 * rather than throw away the one register that survived. */
	*bfar = SCB->BFAR;
}
#else
static void read_fault_registers(uint32_t *cfsr, uint32_t *hfsr, uint32_t *bfar)
{
	*cfsr = 0U;
	*hfsr = 0U;
	*bfar = 0U;
}
#endif

void mcuhome_health_breadcrumb_record(unsigned int reason, const struct arch_esf *esf)
{
	uint32_t cfsr = 0U;
	uint32_t hfsr = 0U;
	uint32_t bfar = 0U;
	uint32_t pc = 0U;
	uint32_t lr = 0U;

	read_fault_registers(&cfsr, &hfsr, &bfar);

#if defined(CONFIG_CPU_CORTEX_M)
	if (esf != NULL) {
		pc = esf->basic.pc;
		lr = esf->basic.lr;
	}
#else
	ARG_UNUSED(esf);
#endif

	mcuhome_breadcrumb_fill(&mcuhome_crash_breadcrumb, (uint32_t)reason, pc, lr, cfsr, hfsr,
				bfar);
}

uint32_t mcuhome_health_fault_count(void)
{
	if (mcuhome_breadcrumb_valid(&mcuhome_crash_breadcrumb, MCUHOME_BREADCRUMB_LIVE) ||
	    mcuhome_breadcrumb_valid(&mcuhome_crash_breadcrumb, MCUHOME_BREADCRUMB_REPORTED)) {
		return mcuhome_crash_breadcrumb.seq;
	}
	return 0U;
}

/* At ERR level and in one place, because the whole value of this record
 * is that somebody reading a boot log cannot miss it. A boot that follows
 * a clean reset says nothing at all — silence here means "the last stop
 * was not a crash", and that is information too. */
static int mcuhome_health_breadcrumb_report(void)
{
	const struct mcuhome_crash_breadcrumb *bc = &mcuhome_crash_breadcrumb;

	if (!mcuhome_breadcrumb_valid(bc, MCUHOME_BREADCRUMB_LIVE)) {
		return 0;
	}

	LOG_ERR("PREVIOUS BOOT ENDED IN A FATAL ERROR (#%u since power-on)", bc->seq);
	LOG_ERR("  reason %u, PC 0x%08x, LR 0x%08x", bc->reason, bc->pc, bc->lr);
	LOG_ERR("  CFSR 0x%08x HFSR 0x%08x BFAR 0x%08x (CFSR is cleared by Zephyr's fault "
		"dump before this is taken; BFAR is not)",
		bc->cfsr, bc->hfsr, bc->bfar);

	mcuhome_breadcrumb_mark_reported(&mcuhome_crash_breadcrumb);
	return 0;
}

/* APPLICATION level: the log core is up, so the lines above are queued
 * like any other and come out in boot order rather than ahead of the
 * banner. Late enough to be readable, early enough to precede anything
 * the application does with the fault count. */
SYS_INIT(mcuhome_health_breadcrumb_report, APPLICATION, CONFIG_APPLICATION_INIT_PRIORITY);
