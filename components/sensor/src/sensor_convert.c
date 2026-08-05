/*
 * SPDX-FileCopyrightText: 2026 The MCUHome Contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * Pure conversion and report-decision arithmetic for the channel layer.
 * See sensor_convert.h for the contract and tests/channel/ for the
 * host-run coverage this file exists to be testable by.
 *
 * Style note: everything works on unsigned magnitudes plus a sign flag
 * rather than on signed values directly. Two reasons, both real:
 * rounding half away from zero is a single expression on a magnitude and
 * a mirrored pair on a signed value, and negating a signed value is
 * undefined behaviour at the type minimum, which is exactly the input a
 * saturation test hands over.
 */

#include <errno.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "sensor_convert.h"

/** Zephyr's sensor_value_to_micro() scale: sensor unit -> micro-units. */
#define MICRO_PER_UNIT 1000000ULL

/** |v| as an unsigned magnitude. Widening first keeps INT32_MIN valid. */
static uint64_t abs_i32(int32_t v)
{
	return (v < 0) ? (uint64_t)(-(int64_t)v) : (uint64_t)v;
}

/** a * b, saturating at UINT64_MAX instead of wrapping. */
static uint64_t mul_sat(uint64_t a, uint64_t b)
{
	if (b != 0U && a > UINT64_MAX / b) {
		return UINT64_MAX;
	}
	return a * b;
}

/**
 * a / b rounded half away from zero (both are magnitudes, so simply
 * rounded half up), saturating instead of wrapping. @p b must be > 0.
 */
static uint64_t div_round_sat(uint64_t a, uint64_t b)
{
	const uint64_t half = b / 2U;

	if (a > UINT64_MAX - half) {
		return UINT64_MAX;
	}
	return (a + half) / b;
}

int mcuhome_channel_value_range(enum mcuhome_attr_type type, int64_t *min, int64_t *max)
{
	if (min == NULL || max == NULL) {
		return -EINVAL;
	}

	switch (type) {
	case MCUHOME_ATTR_TYPE_INT16S:
		*min = (int64_t)INT16_MIN + 1;
		*max = (int64_t)INT16_MAX;
		return 0;
	case MCUHOME_ATTR_TYPE_INT16U:
		*min = 0;
		*max = (int64_t)UINT16_MAX - 1;
		return 0;
	case MCUHOME_ATTR_TYPE_INT32S:
		*min = (int64_t)INT32_MIN + 1;
		*max = (int64_t)INT32_MAX;
		return 0;
	case MCUHOME_ATTR_TYPE_INT32U:
		*min = 0;
		*max = (int64_t)UINT32_MAX - 1;
		return 0;
	case MCUHOME_ATTR_TYPE_BOOL:
		break;
	}

	return -ENOTSUP;
}

int mcuhome_channel_convert(int64_t micro, int32_t num, int32_t den, int32_t offset,
			    enum mcuhome_attr_type type, int64_t *out)
{
	uint64_t magnitude;
	uint64_t quotient;
	int64_t val;
	int64_t min;
	int64_t max;
	bool negative;
	int rc;

	if (out == NULL || den == 0) {
		return -EINVAL;
	}

	rc = mcuhome_channel_value_range(type, &min, &max);
	if (rc != 0) {
		return rc;
	}

	negative = (num < 0) != (den < 0);
	if (micro < 0) {
		negative = !negative;
		/* Two's-complement negation of the unsigned reinterpretation:
		 * the one way to take |INT64_MIN| without UB. */
		magnitude = ~(uint64_t)micro + 1U;
	} else {
		magnitude = (uint64_t)micro;
	}

	quotient = div_round_sat(mul_sat(magnitude, abs_i32(num)),
				 mul_sat(abs_i32(den), MICRO_PER_UNIT));

	if (quotient > (uint64_t)INT64_MAX) {
		quotient = (uint64_t)INT64_MAX;
	}
	val = negative ? -(int64_t)quotient : (int64_t)quotient;

	/* Offset is a plain int32 in raw units, so this only ever trips on
	 * an already-saturated quotient — but an int64 overflow here would
	 * flip the sign and turn a saturated maximum into a minimum. */
	if (offset > 0 && val > INT64_MAX - offset) {
		val = INT64_MAX;
	} else if (offset < 0 && val < INT64_MIN - offset) {
		val = INT64_MIN;
	} else {
		val += offset;
	}

	if (val < min) {
		*out = min;
		return -ERANGE;
	}
	if (val > max) {
		*out = max;
		return -ERANGE;
	}

	*out = val;
	return 0;
}

bool mcuhome_channel_needs_report(int64_t raw, int64_t last, bool has_last, uint32_t delta)
{
	uint64_t diff;

	if (!has_last) {
		return true;
	}

	/* Both operands come out of mcuhome_channel_convert() and are
	 * therefore inside a range narrower than int32; the subtraction
	 * cannot overflow. Taken as a magnitude for the reason in the file
	 * header. */
	diff = (raw >= last) ? (uint64_t)(raw - last) : (uint64_t)(last - raw);

	return diff >= (uint64_t)delta;
}
