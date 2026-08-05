/*
 * SPDX-FileCopyrightText: 2026 The MCUHome Contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * native_sim coverage of the channel layer's arithmetic
 * (components/sensor/src/sensor_convert.c): unit conversion, rounding,
 * saturation, the reserved Matter null encodings, and the report-on-delta
 * decision.
 *
 * The inputs are the `micro` values Zephyr's sensor_value_to_micro()
 * produces — `val1 * 1000000 + val2`, with both fields carrying the sign
 * for negative readings — so the cases below can be read as physical
 * quantities: 21500000 is 21.5 of whatever unit the driver reports.
 *
 * Everything here runs on the host. That is the whole reason the
 * arithmetic lives in its own translation unit: the poller around it
 * needs devices, a workqueue and the Matter reporting path, none of which
 * exist on native_sim, while none of them influence a single value below.
 */

#include <errno.h>
#include <stddef.h>
#include <stdint.h>

#include <zephyr/sys/util.h>
#include <zephyr/ztest.h>

#include <mcuhome/matter_tables.h>

#include "sensor_convert.h"

/* The two scales the sample node uses, as the bindings spell them out. */
#define CELSIUS_TO_CENTI_NUM 100 /* Zephyr °C   -> Matter 0.01 °C  */
#define CELSIUS_TO_CENTI_DEN 1
#define KPA_TO_DECI_NUM      10 /* Zephyr kPa  -> Matter 0.1 kPa */
#define KPA_TO_DECI_DEN      1

/* --- mcuhome_channel_value_range() --------------------------------------
 *
 * The ranges are one narrower than the C types at the end where CHIP
 * reserves the null encoding. That asymmetry is the point of the function
 * and the reason it is tested at all.
 */

ZTEST(channel_convert, test_range_int16s_excludes_null_marker)
{
	int64_t min;
	int64_t max;

	zassert_equal(0, mcuhome_channel_value_range(MCUHOME_ATTR_TYPE_INT16S, &min, &max));
	zassert_equal(-32767, min, "INT16_MIN is CHIP's null marker and must not be publishable");
	zassert_equal(32767, max);
}

ZTEST(channel_convert, test_range_int16u_excludes_null_marker)
{
	int64_t min;
	int64_t max;

	zassert_equal(0, mcuhome_channel_value_range(MCUHOME_ATTR_TYPE_INT16U, &min, &max));
	zassert_equal(0, min);
	zassert_equal(65534, max, "UINT16_MAX is CHIP's null marker");
}

ZTEST(channel_convert, test_range_int32s_excludes_null_marker)
{
	int64_t min;
	int64_t max;

	zassert_equal(0, mcuhome_channel_value_range(MCUHOME_ATTR_TYPE_INT32S, &min, &max));
	zassert_equal((int64_t)INT32_MIN + 1, min);
	zassert_equal((int64_t)INT32_MAX, max);
}

ZTEST(channel_convert, test_range_int32u_excludes_null_marker)
{
	int64_t min;
	int64_t max;

	zassert_equal(0, mcuhome_channel_value_range(MCUHOME_ATTR_TYPE_INT32U, &min, &max));
	zassert_equal(0, min);
	zassert_equal((int64_t)UINT32_MAX - 1, max);
}

ZTEST(channel_convert, test_range_bool_rejected)
{
	int64_t min;
	int64_t max;

	zassert_equal(-ENOTSUP, mcuhome_channel_value_range(MCUHOME_ATTR_TYPE_BOOL, &min, &max),
		      "a boolean is not a sensor channel type");
}

ZTEST(channel_convert, test_range_null_output_rejected)
{
	int64_t v;

	zassert_equal(-EINVAL, mcuhome_channel_value_range(MCUHOME_ATTR_TYPE_INT16S, NULL, &v));
	zassert_equal(-EINVAL, mcuhome_channel_value_range(MCUHOME_ATTR_TYPE_INT16S, &v, NULL));
}

/* --- mcuhome_channel_convert(): the real scales -------------------------- */

ZTEST(channel_convert, test_temperature_scale)
{
	int64_t raw;

	/* 21.5 °C -> 2150 (0.01 °C) */
	zassert_equal(0,
		      mcuhome_channel_convert(21500000, CELSIUS_TO_CENTI_NUM, CELSIUS_TO_CENTI_DEN,
					      0, MCUHOME_ATTR_TYPE_INT16S, &raw));
	zassert_equal(2150, raw);
}

ZTEST(channel_convert, test_negative_temperature_scale)
{
	int64_t raw;

	/* -12.34 °C: sensor_value carries the sign in both fields, so
	 * val1 = -12, val2 = -340000 -> micro = -12340000. */
	zassert_equal(0,
		      mcuhome_channel_convert(-12340000, CELSIUS_TO_CENTI_NUM, CELSIUS_TO_CENTI_DEN,
					      0, MCUHOME_ATTR_TYPE_INT16S, &raw));
	zassert_equal(-1234, raw);
}

ZTEST(channel_convert, test_pressure_scale)
{
	int64_t raw;

	/* 101.325 kPa -> 1013.25 -> 1013 (0.1 kPa == 1 hPa) */
	zassert_equal(0, mcuhome_channel_convert(101325000, KPA_TO_DECI_NUM, KPA_TO_DECI_DEN, 0,
						 MCUHOME_ATTR_TYPE_INT16S, &raw));
	zassert_equal(1013, raw);
}

ZTEST(channel_convert, test_pressure_within_bmp180_range)
{
	int64_t raw;

	/* The two ends of the BMP180's datasheet range, which the sample's
	 * Min/MaxMeasuredValue attributes advertise: 300 and 1100 hPa. */
	zassert_equal(0, mcuhome_channel_convert(30000000, KPA_TO_DECI_NUM, KPA_TO_DECI_DEN, 0,
						 MCUHOME_ATTR_TYPE_INT16S, &raw));
	zassert_equal(300, raw);

	zassert_equal(0, mcuhome_channel_convert(110000000, KPA_TO_DECI_NUM, KPA_TO_DECI_DEN, 0,
						 MCUHOME_ATTR_TYPE_INT16S, &raw));
	zassert_equal(1100, raw);
}

/* --- Rounding ------------------------------------------------------------
 *
 * Half away from zero, not truncation: truncating would pull every value
 * toward zero and show up as a systematic half-LSB bias with opposite
 * signs above and below 0 °C.
 */

ZTEST(channel_convert, test_rounds_half_away_from_zero_positive)
{
	int64_t raw;

	/* +0.005 °C is exactly half an LSB -> 1, not 0. */
	zassert_equal(0, mcuhome_channel_convert(5000, CELSIUS_TO_CENTI_NUM, CELSIUS_TO_CENTI_DEN,
						 0, MCUHOME_ATTR_TYPE_INT16S, &raw));
	zassert_equal(1, raw);
}

ZTEST(channel_convert, test_rounds_half_away_from_zero_negative)
{
	int64_t raw;

	/* -0.005 °C -> -1, mirroring the positive case exactly. */
	zassert_equal(0, mcuhome_channel_convert(-5000, CELSIUS_TO_CENTI_NUM, CELSIUS_TO_CENTI_DEN,
						 0, MCUHOME_ATTR_TYPE_INT16S, &raw));
	zassert_equal(-1, raw);
}

ZTEST(channel_convert, test_rounds_below_half_toward_zero)
{
	int64_t raw;

	/* +0.00499 °C -> 0, and its mirror -> 0 as well (not -1). */
	zassert_equal(0, mcuhome_channel_convert(4990, CELSIUS_TO_CENTI_NUM, CELSIUS_TO_CENTI_DEN,
						 0, MCUHOME_ATTR_TYPE_INT16S, &raw));
	zassert_equal(0, raw);

	zassert_equal(0, mcuhome_channel_convert(-4990, CELSIUS_TO_CENTI_NUM, CELSIUS_TO_CENTI_DEN,
						 0, MCUHOME_ATTR_TYPE_INT16S, &raw));
	zassert_equal(0, raw);
}

/* --- Scale factor variants ----------------------------------------------- */

ZTEST(channel_convert, test_denominator_divides)
{
	int64_t raw;

	/* num/den = 1/2: a sensor reporting in units twice the attribute's. */
	zassert_equal(0, mcuhome_channel_convert(7000000, 1, 2, 0, MCUHOME_ATTR_TYPE_INT16S, &raw));
	zassert_equal(4, raw, "3.5 rounds away from zero to 4");
}

ZTEST(channel_convert, test_negative_numerator_flips_sign)
{
	int64_t raw;

	zassert_equal(0,
		      mcuhome_channel_convert(21500000, -CELSIUS_TO_CENTI_NUM, CELSIUS_TO_CENTI_DEN,
					      0, MCUHOME_ATTR_TYPE_INT16S, &raw));
	zassert_equal(-2150, raw);
}

ZTEST(channel_convert, test_negative_denominator_flips_sign)
{
	int64_t raw;

	zassert_equal(0, mcuhome_channel_convert(21500000, CELSIUS_TO_CENTI_NUM, -1, 0,
						 MCUHOME_ATTR_TYPE_INT16S, &raw));
	zassert_equal(-2150, raw);
}

ZTEST(channel_convert, test_two_negatives_stay_positive)
{
	int64_t raw;

	zassert_equal(0, mcuhome_channel_convert(-21500000, -CELSIUS_TO_CENTI_NUM, 1, 0,
						 MCUHOME_ATTR_TYPE_INT16S, &raw));
	zassert_equal(2150, raw);
}

ZTEST(channel_convert, test_zero_numerator_yields_offset_only)
{
	int64_t raw;

	zassert_equal(0,
		      mcuhome_channel_convert(21500000, 0, 1, 42, MCUHOME_ATTR_TYPE_INT16S, &raw));
	zassert_equal(42, raw);
}

/* --- Offset --------------------------------------------------------------
 *
 * Applied AFTER scaling, and therefore in raw attribute units. Getting
 * that order wrong is silent and produces plausible values, so it gets its
 * own test.
 */

ZTEST(channel_convert, test_offset_is_applied_after_scaling)
{
	int64_t raw;

	zassert_equal(0,
		      mcuhome_channel_convert(21500000, CELSIUS_TO_CENTI_NUM, CELSIUS_TO_CENTI_DEN,
					      -100, MCUHOME_ATTR_TYPE_INT16S, &raw));
	zassert_equal(2050, raw, "offset is 1.00 °C in raw units, not 100 °C in sensor units");
}

ZTEST(channel_convert, test_offset_alone_on_zero_reading)
{
	int64_t raw;

	zassert_equal(0, mcuhome_channel_convert(0, CELSIUS_TO_CENTI_NUM, CELSIUS_TO_CENTI_DEN, 273,
						 MCUHOME_ATTR_TYPE_INT16S, &raw));
	zassert_equal(273, raw);
}

/* --- Clamping ------------------------------------------------------------ */

ZTEST(channel_convert, test_clamps_above_int16s_max)
{
	int64_t raw;

	/* 400 °C -> 40000, past INT16_MAX. */
	zassert_equal(-ERANGE,
		      mcuhome_channel_convert(400000000, CELSIUS_TO_CENTI_NUM, CELSIUS_TO_CENTI_DEN,
					      0, MCUHOME_ATTR_TYPE_INT16S, &raw));
	zassert_equal(32767, raw, "clamped value is still returned, so the caller can publish it");
}

ZTEST(channel_convert, test_clamps_below_int16s_min_without_hitting_null)
{
	int64_t raw;

	zassert_equal(-ERANGE, mcuhome_channel_convert(-400000000, CELSIUS_TO_CENTI_NUM,
						       CELSIUS_TO_CENTI_DEN, 0,
						       MCUHOME_ATTR_TYPE_INT16S, &raw));
	zassert_equal(-32767, raw, "a saturated sensor must not encode as null on the wire");
}

ZTEST(channel_convert, test_clamps_negative_reading_on_unsigned_channel)
{
	int64_t raw;

	zassert_equal(-ERANGE,
		      mcuhome_channel_convert(-1000000, 1, 1, 0, MCUHOME_ATTR_TYPE_INT16U, &raw));
	zassert_equal(0, raw);
}

ZTEST(channel_convert, test_clamps_above_int16u_max_without_hitting_null)
{
	int64_t raw;

	zassert_equal(-ERANGE, mcuhome_channel_convert(100000000000LL, 1, 1, 0,
						       MCUHOME_ATTR_TYPE_INT16U, &raw));
	zassert_equal(65534, raw);
}

ZTEST(channel_convert, test_wide_value_fits_int32u)
{
	int64_t raw;

	/* 3_000_000_000 does not fit int32s but does fit int32u — the case
	 * that makes the conversion path 64-bit throughout. */
	zassert_equal(0, mcuhome_channel_convert(3000000000000000LL, 1, 1, 0,
						 MCUHOME_ATTR_TYPE_INT32U, &raw));
	zassert_equal(3000000000LL, raw);
}

ZTEST(channel_convert, test_extreme_input_saturates_instead_of_wrapping)
{
	int64_t raw;

	/* INT64_MIN is the input that makes a naive `-micro` undefined and a
	 * naive `micro * num` wrap into a large POSITIVE number — i.e. a
	 * freezing sensor reporting as scorching. */
	zassert_equal(-ERANGE, mcuhome_channel_convert(INT64_MIN, CELSIUS_TO_CENTI_NUM, 1, 0,
						       MCUHOME_ATTR_TYPE_INT16S, &raw));
	zassert_equal(-32767, raw);

	zassert_equal(-ERANGE, mcuhome_channel_convert(INT64_MAX, CELSIUS_TO_CENTI_NUM, 1, 0,
						       MCUHOME_ATTR_TYPE_INT16S, &raw));
	zassert_equal(32767, raw);
}

/* --- Argument validation ------------------------------------------------- */

ZTEST(channel_convert, test_zero_denominator_rejected)
{
	int64_t raw;

	zassert_equal(-EINVAL,
		      mcuhome_channel_convert(21500000, 100, 0, 0, MCUHOME_ATTR_TYPE_INT16S, &raw));
}

ZTEST(channel_convert, test_null_output_rejected)
{
	zassert_equal(-EINVAL,
		      mcuhome_channel_convert(21500000, 100, 1, 0, MCUHOME_ATTR_TYPE_INT16S, NULL));
}

ZTEST(channel_convert, test_bool_channel_rejected)
{
	int64_t raw;

	zassert_equal(-ENOTSUP,
		      mcuhome_channel_convert(1000000, 1, 1, 0, MCUHOME_ATTR_TYPE_BOOL, &raw));
}

/* --- mcuhome_channel_needs_report() -------------------------------------- */

ZTEST(channel_convert, test_first_sample_always_reports)
{
	zassert_true(mcuhome_channel_needs_report(2150, 0, false, 10),
		     "before the first publish the attribute reads null - any value is news");
	zassert_true(mcuhome_channel_needs_report(0, 0, false, UINT32_MAX));
}

ZTEST(channel_convert, test_zero_delta_reports_every_sample)
{
	zassert_true(mcuhome_channel_needs_report(2150, 2150, true, 0));
}

ZTEST(channel_convert, test_change_below_delta_is_suppressed)
{
	zassert_false(mcuhome_channel_needs_report(2159, 2150, true, 10));
	zassert_false(mcuhome_channel_needs_report(2141, 2150, true, 10));
	zassert_false(mcuhome_channel_needs_report(2150, 2150, true, 10));
}

ZTEST(channel_convert, test_change_at_delta_reports)
{
	zassert_true(mcuhome_channel_needs_report(2160, 2150, true, 10),
		     "the threshold is inclusive");
	zassert_true(mcuhome_channel_needs_report(2140, 2150, true, 10));
}

ZTEST(channel_convert, test_delta_is_symmetric_across_zero)
{
	/* -0.05 °C vs +0.05 °C is a 0.10 °C change either way. */
	zassert_true(mcuhome_channel_needs_report(5, -5, true, 10));
	zassert_true(mcuhome_channel_needs_report(-5, 5, true, 10));
	zassert_false(mcuhome_channel_needs_report(4, -5, true, 10));
}

ZTEST(channel_convert, test_delta_over_full_int32_span)
{
	/* The widest difference a channel can produce still has to compare
	 * correctly against a delta near UINT32_MAX. */
	zassert_true(
		mcuhome_channel_needs_report(0, (int64_t)UINT32_MAX - 1, true, UINT32_MAX - 1));
	zassert_false(
		mcuhome_channel_needs_report(0, (int64_t)UINT32_MAX - 2, true, UINT32_MAX - 1));
}

/* --- Sequences -----------------------------------------------------------
 *
 * The two units end to end, as the sample's channels see them: convert,
 * then decide. Guards the pair against a change that keeps both halves
 * individually correct but no longer composes.
 */

ZTEST(channel_convert, test_temperature_sequence)
{
	int64_t previous = 0;
	bool has_previous = false;
	/* 21.50, 21.54, 21.61, 21.61 °C — one suppressed, one reported. */
	static const int64_t micro[] = {21500000, 21540000, 21610000, 21610000};
	static const bool expect_report[] = {true, false, true, false};

	for (size_t i = 0; i < ARRAY_SIZE(micro); i++) {
		int64_t raw;
		bool report;

		zassert_equal(0, mcuhome_channel_convert(micro[i], CELSIUS_TO_CENTI_NUM,
							 CELSIUS_TO_CENTI_DEN, 0,
							 MCUHOME_ATTR_TYPE_INT16S, &raw));
		report = mcuhome_channel_needs_report(raw, previous, has_previous, 10);
		zassert_equal(expect_report[i], report, "sample %u", (unsigned int)i);

		if (report) {
			previous = raw;
			has_previous = true;
		}
	}

	zassert_equal(2161, previous);
}

ZTEST(channel_convert, test_pressure_sequence)
{
	int64_t previous = 0;
	bool has_previous = false;
	/* 101.325, 101.36, 101.44 kPa -> 1013, 1014, 1014 hPa with a 1 hPa
	 * delta: the middle sample crosses, the last one does not. */
	static const int64_t micro[] = {101325000, 101360000, 101440000};
	static const bool expect_report[] = {true, true, false};

	for (size_t i = 0; i < ARRAY_SIZE(micro); i++) {
		int64_t raw;
		bool report;

		zassert_equal(0, mcuhome_channel_convert(micro[i], KPA_TO_DECI_NUM, KPA_TO_DECI_DEN,
							 0, MCUHOME_ATTR_TYPE_INT16S, &raw));
		report = mcuhome_channel_needs_report(raw, previous, has_previous, 1);
		zassert_equal(expect_report[i], report, "sample %u", (unsigned int)i);

		if (report) {
			previous = raw;
			has_previous = true;
		}
	}

	zassert_equal(1014, previous);
}

ZTEST_SUITE(channel_convert, NULL, NULL, NULL, NULL, NULL);
