/*
 * SPDX-FileCopyrightText: 2026 The MCUHome Contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * Does the crash breadcrumb survive, and does it ever lie?
 *
 * Those are the only two questions worth asking about a record that
 * lives in uninitialized RAM. "Survives" is the easy half and the tests
 * for it are short. "Never lies" is the half that matters: after a
 * power-up the record's memory holds whatever it holds, and on this
 * project's targets MCUboot has used the same RAM before the application
 * ever runs. A breadcrumb that reports a fault the device did not have
 * is worse than no breadcrumb at all — it would send somebody hunting a
 * crash that never happened, and it would make every later report
 * suspect. So most of what follows is about junk being rejected.
 */

#include <string.h>

#include <zephyr/logging/log.h>
#include <zephyr/ztest.h>

#include <mcuhome/health.h>

#include "breadcrumb.h"
#include "breadcrumb_core.h"

/* breadcrumb.c only DECLAREs this module — in a real image health.c
 * registers it. Here the suite stands in for health.c. */
LOG_MODULE_REGISTER(mcuhome_health, CONFIG_LOG_DEFAULT_LEVEL);

/* The real record breadcrumb.c writes into, so the tests below can look
 * at what the shipping code actually produced. */
extern struct mcuhome_crash_breadcrumb mcuhome_crash_breadcrumb;

/* Values with no special meaning, chosen to be recognizable in a failure
 * message and to have bits set all over the word. */
#define A_REASON 4U
#define A_PC     0x0002FE71U
#define A_LR     0x0002FF35U
#define A_CFSR   0x00008200U
#define A_HFSR   0x40000000U
#define A_BFAR   0x20001234U

static struct mcuhome_crash_breadcrumb bc;

static void record_one(void)
{
	mcuhome_breadcrumb_fill(&bc, A_REASON, A_PC, A_LR, A_CFSR, A_HFSR, A_BFAR);
}

/** Whatever was in that RAM before anybody wrote a record. */
static void fill_with_junk(uint8_t pattern)
{
	memset(&bc, pattern, sizeof(bc));
}

static void test_before(void *fixture)
{
	ARG_UNUSED(fixture);
	fill_with_junk(0x00);
}

ZTEST_SUITE(mcuhome_health_breadcrumb, NULL, NULL, test_before, NULL, NULL);

/* --- Surviving ---------------------------------------------------------- */

ZTEST(mcuhome_health_breadcrumb, test_a_recorded_fault_reads_back_intact)
{
	record_one();

	zassert_true(mcuhome_breadcrumb_valid(&bc, MCUHOME_BREADCRUMB_LIVE),
		     "a record just written has to be valid and live");
	zassert_equal(bc.reason, A_REASON, "reason");
	zassert_equal(bc.pc, A_PC, "pc");
	zassert_equal(bc.lr, A_LR, "lr");
	zassert_equal(bc.cfsr, A_CFSR, "cfsr");
	zassert_equal(bc.hfsr, A_HFSR, "hfsr");
	zassert_equal(bc.bfar, A_BFAR, "bfar");
	zassert_equal(bc.seq, 1U, "the first fault since power-on is fault 1");
}

/* Reporting must not destroy the count: a device that crashes, reports,
 * runs for a week and crashes again is on fault 2, not fault 1. */
ZTEST(mcuhome_health_breadcrumb, test_reporting_keeps_the_record_countable)
{
	record_one();
	mcuhome_breadcrumb_mark_reported(&bc);

	zassert_false(mcuhome_breadcrumb_valid(&bc, MCUHOME_BREADCRUMB_LIVE),
		      "a reported record must not be reported a second time");
	zassert_true(mcuhome_breadcrumb_valid(&bc, MCUHOME_BREADCRUMB_REPORTED),
		     "a reported record is still a valid record");
	zassert_equal(bc.seq, 1U, "reporting must not change the count");

	mcuhome_breadcrumb_fill(&bc, A_REASON, A_PC, A_LR, 0U, 0U, 0U);
	zassert_equal(bc.seq, 2U, "the fault after a reported one is fault 2");
	zassert_true(mcuhome_breadcrumb_valid(&bc, MCUHOME_BREADCRUMB_LIVE),
		     "the new record is live again");
}

/* Two faults without a boot in between — the reset-loop case, which is
 * the one where the count is the whole diagnosis. */
ZTEST(mcuhome_health_breadcrumb, test_unreported_faults_keep_counting)
{
	record_one();
	record_one();
	record_one();

	zassert_equal(bc.seq, 3U, "an unreported record still carries the count forward");
}

/* --- Never lying -------------------------------------------------------- */

ZTEST(mcuhome_health_breadcrumb, test_junk_is_not_a_crash_report)
{
	const uint8_t patterns[] = {0x00, 0xFF, 0xA5, 0x5A};

	for (size_t i = 0; i < ARRAY_SIZE(patterns); i++) {
		fill_with_junk(patterns[i]);
		zassert_false(mcuhome_breadcrumb_valid(&bc, MCUHOME_BREADCRUMB_LIVE),
			      "uninitialized RAM filled with 0x%02x must not read as a "
			      "live crash report",
			      patterns[i]);
		zassert_false(mcuhome_breadcrumb_valid(&bc, MCUHOME_BREADCRUMB_REPORTED),
			      "uninitialized RAM filled with 0x%02x must not read as a "
			      "reported crash report",
			      patterns[i]);
	}
}

/* The reason the record carries a checksum and not only a magic word: a
 * magic can survive by accident in RAM somebody else used. */
ZTEST(mcuhome_health_breadcrumb, test_the_magic_alone_does_not_validate_a_record)
{
	fill_with_junk(0xA5);
	bc.state = MCUHOME_BREADCRUMB_LIVE;

	zassert_false(mcuhome_breadcrumb_valid(&bc, MCUHOME_BREADCRUMB_LIVE),
		      "a magic word on top of junk is not a crash report");
}

ZTEST(mcuhome_health_breadcrumb, test_a_single_flipped_bit_invalidates_the_record)
{
	uint32_t *const fields[] = {&bc.state, &bc.seq,  &bc.reason, &bc.pc,
				    &bc.lr,    &bc.cfsr, &bc.hfsr,   &bc.bfar};

	for (size_t i = 0; i < ARRAY_SIZE(fields); i++) {
		for (unsigned int bit = 0; bit < 32U; bit += 7U) {
			record_one();
			*fields[i] ^= (1U << bit);

			zassert_false(mcuhome_breadcrumb_valid(&bc, MCUHOME_BREADCRUMB_LIVE),
				      "field %zu with bit %u flipped still validated — the "
				      "integrity word has to cover every field",
				      i, bit);
		}
	}
}

ZTEST(mcuhome_health_breadcrumb, test_a_corrupt_checksum_invalidates_the_record)
{
	record_one();
	bc.check = ~bc.check;

	zassert_false(mcuhome_breadcrumb_valid(&bc, MCUHOME_BREADCRUMB_LIVE),
		      "a record whose checksum does not match must be dropped");
}

/* A clobbered record must not poison the count either: the next fault
 * starts over at 1 rather than continuing from a number nobody can
 * trust. */
ZTEST(mcuhome_health_breadcrumb, test_a_clobbered_record_restarts_the_count)
{
	record_one();
	record_one();
	zassert_equal(bc.seq, 2U, "precondition");

	bc.check ^= 1U;
	record_one();

	zassert_equal(bc.seq, 1U,
		      "a fault recorded over an unreadable record is the first one this "
		      "record knows about, not the third");
	zassert_true(mcuhome_breadcrumb_valid(&bc, MCUHOME_BREADCRUMB_LIVE),
		     "and it has to be a valid record afterwards");
}

/* The two states must not be confusable: asking for one and getting the
 * other would report an old crash again on every boot. */
ZTEST(mcuhome_health_breadcrumb, test_the_two_states_are_distinct)
{
	zassert_not_equal(MCUHOME_BREADCRUMB_LIVE, MCUHOME_BREADCRUMB_REPORTED,
			  "live and reported have to be different words");

	record_one();
	zassert_false(mcuhome_breadcrumb_valid(&bc, MCUHOME_BREADCRUMB_REPORTED),
		      "a live record must not answer to the reported state");
}

/* An all-zero record is the one junk pattern most likely to occur: it is
 * what a bootloader's .bss leaves behind. */
ZTEST(mcuhome_health_breadcrumb, test_a_zeroed_record_with_a_matching_checksum_is_still_rejected)
{
	memset(&bc, 0, sizeof(bc));
	bc.check = mcuhome_breadcrumb_check(&bc);

	zassert_false(mcuhome_breadcrumb_valid(&bc, MCUHOME_BREADCRUMB_LIVE),
		      "state 0 is not a state this record ever writes");
	zassert_false(mcuhome_breadcrumb_valid(&bc, MCUHOME_BREADCRUMB_REPORTED),
		      "state 0 is not a state this record ever writes");
}

/* --- The shipping file, not only the design ----------------------------- */

/*
 * These drive lib/health/breadcrumb.c itself: its own `__noinit` record,
 * the recording entry point the fatal handler calls, and the count the
 * rest of the framework reads through <mcuhome/health.h>. What they
 * cannot cover is whether that RAM survives a real reset — no host test
 * can — which is exactly why the record is checksummed rather than
 * trusted.
 */

ZTEST(mcuhome_health_breadcrumb, test_the_shipping_recorder_produces_a_readable_record)
{
	memset(&mcuhome_crash_breadcrumb, 0xC5, sizeof(mcuhome_crash_breadcrumb));
	zassert_equal(mcuhome_health_fault_count(), 0U,
		      "junk in the record must not be reported as a fault count");

	mcuhome_health_breadcrumb_record(A_REASON, NULL);

	zassert_true(mcuhome_breadcrumb_valid(&mcuhome_crash_breadcrumb, MCUHOME_BREADCRUMB_LIVE),
		     "the fatal handler's record has to read back as a live crash report");
	zassert_equal(mcuhome_crash_breadcrumb.reason, A_REASON, "reason");
	zassert_equal(mcuhome_health_fault_count(), 1U, "the first fault since power-on");

	mcuhome_health_breadcrumb_record(A_REASON, NULL);
	zassert_equal(mcuhome_health_fault_count(), 2U, "the count has to survive in place");
}

ZTEST(mcuhome_health_breadcrumb, test_a_null_exception_frame_is_recorded_not_dereferenced)
{
	memset(&mcuhome_crash_breadcrumb, 0, sizeof(mcuhome_crash_breadcrumb));

	/* A fatal error raised without an exception — k_panic(), an assert —
	 * arrives with esf == NULL, and a fault handler that dereferenced it
	 * would fault inside the fault handler. */
	mcuhome_health_breadcrumb_record(A_REASON, NULL);

	zassert_true(mcuhome_breadcrumb_valid(&mcuhome_crash_breadcrumb, MCUHOME_BREADCRUMB_LIVE),
		     "a frameless fatal error is still worth recording");
	zassert_equal(mcuhome_crash_breadcrumb.pc, 0U, "no frame means no PC, not a wild read");
	zassert_equal(mcuhome_crash_breadcrumb.lr, 0U, "no frame means no LR, not a wild read");
}
