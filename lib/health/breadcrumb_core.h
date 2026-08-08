/*
 * SPDX-FileCopyrightText: 2026 The MCUHome Contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * The crash breadcrumb's pure logic: a small record in uninitialized
 * (reset-surviving) RAM, an integrity word over it, and the sequence
 * rule for counting consecutive faults. Everything here is plain C over
 * a caller-provided struct — no Zephyr, no registers, no sections — so
 * the native_sim suite in tests/health_breadcrumb can drive every branch
 * on the host. The one file that talks to hardware and linker sections
 * is breadcrumb.c next to this.
 *
 * Why an integrity word instead of trusting a magic alone: the record
 * lives in RAM that nothing initializes on purpose. After a power-up it
 * holds junk, and on this project's nRF5340 targets the bootloader owns
 * the same RAM before the application does. A magic word can survive
 * either by accident; a magic plus a mixed checksum over every field
 * cannot, short of deliberate effort. A clobbered breadcrumb is silently
 * dropped — losing a report is acceptable, inventing one is not.
 */

#ifndef MCUHOME_LIB_HEALTH_BREADCRUMB_CORE_H_
#define MCUHOME_LIB_HEALTH_BREADCRUMB_CORE_H_

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/** A fatal error was recorded and has not been reported yet. */
#define MCUHOME_BREADCRUMB_LIVE 0x42D0FA7AU
/** The record was reported at boot; kept only so the fault count
 *  survives until the next fault or until power is lost. */
#define MCUHOME_BREADCRUMB_REPORTED 0x42D05EE2U

/** One crash, as much of it as a fault handler can save with plain
 *  stores. The three SCB registers are zero on targets that do not have
 *  them (native_sim), and may legitimately read zero on Cortex-M too:
 *  Zephyr's fault path clears the sticky CFSR bits after printing them,
 *  before this record is written. PC, LR and the reason come from the
 *  exception stack frame and are always real. */
struct mcuhome_crash_breadcrumb {
	uint32_t state; /**< MCUHOME_BREADCRUMB_LIVE / _REPORTED. */
	uint32_t seq;   /**< Fault count since this RAM last lost power. */
	uint32_t reason; /**< The k_fatal_error reason code. */
	uint32_t pc;
	uint32_t lr;
	uint32_t cfsr;
	uint32_t hfsr;
	uint32_t bfar;
	uint32_t check; /**< mcuhome_breadcrumb_check() over all of the above. */
};

/** The integrity word: every field mixed so that no single stuck or
 *  clobbered word keeps the record valid. Constants are the usual
 *  Knuth/Weyl multiplicative hashing pair; nothing about this is
 *  cryptographic and nothing needs to be. */
static inline uint32_t mcuhome_breadcrumb_check(const struct mcuhome_crash_breadcrumb *bc)
{
	const uint32_t words[8] = {bc->state, bc->seq, bc->reason, bc->pc,
				   bc->lr,    bc->cfsr, bc->hfsr,  bc->bfar};
	uint32_t check = 0x9E3779B9U;

	for (unsigned int i = 0; i < 8U; i++) {
		check = (check ^ words[i]) * 2654435761U;
		check ^= check >> 16;
	}
	return check;
}

/** Whether @p bc holds an intact record in @p state. */
static inline bool mcuhome_breadcrumb_valid(const struct mcuhome_crash_breadcrumb *bc,
					    uint32_t state)
{
	return bc->state == state && bc->check == mcuhome_breadcrumb_check(bc);
}

/**
 * Record a fatal error into @p bc, in place, with plain stores only —
 * this runs inside the fault handler and must not be able to fail, wait
 * or fault. The sequence number continues from an intact previous
 * record (live or already reported) and restarts at 1 over junk, which
 * is what makes it "faults since power loss".
 */
static inline void mcuhome_breadcrumb_fill(struct mcuhome_crash_breadcrumb *bc, uint32_t reason,
					   uint32_t pc, uint32_t lr, uint32_t cfsr, uint32_t hfsr,
					   uint32_t bfar)
{
	uint32_t seq = 1U;

	if (mcuhome_breadcrumb_valid(bc, MCUHOME_BREADCRUMB_LIVE) ||
	    mcuhome_breadcrumb_valid(bc, MCUHOME_BREADCRUMB_REPORTED)) {
		seq = bc->seq + 1U;
	}

	bc->state = MCUHOME_BREADCRUMB_LIVE;
	bc->seq = seq;
	bc->reason = reason;
	bc->pc = pc;
	bc->lr = lr;
	bc->cfsr = cfsr;
	bc->hfsr = hfsr;
	bc->bfar = bfar;
	bc->check = mcuhome_breadcrumb_check(bc);
}

/** Downgrade a live record to reported, keeping the fault count. */
static inline void mcuhome_breadcrumb_mark_reported(struct mcuhome_crash_breadcrumb *bc)
{
	bc->state = MCUHOME_BREADCRUMB_REPORTED;
	bc->check = mcuhome_breadcrumb_check(bc);
}

#ifdef __cplusplus
}
#endif

#endif /* MCUHOME_LIB_HEALTH_BREADCRUMB_CORE_H_ */
