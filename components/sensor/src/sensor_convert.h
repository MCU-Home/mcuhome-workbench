/*
 * SPDX-FileCopyrightText: 2026 The MCUHome Contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * The arithmetic half of the channel layer, factored out of the poller.
 *
 * Everything here is pure: no CHIP, no Zephyr, no device access, no
 * state. That is deliberate — it is the part with the interesting edge
 * cases (rounding direction, saturation, the reserved Matter null
 * markers, the delta decision) and the part worth exhaustive host-run
 * coverage, which tests/channel/ provides on native_sim.
 *
 * The poller (sensor_poll.c) keeps everything impure: devices, the
 * workqueue, fault latching, the attr-store publish.
 */

#ifndef MCUHOME_COMPONENTS_SENSOR_SENSOR_CONVERT_H_
#define MCUHOME_COMPONENTS_SENSOR_SENSOR_CONVERT_H_

#include <stdbool.h>
#include <stdint.h>

#include <mcuhome/matter_tables.h>

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief Value range a channel of @p type may publish.
 *
 * NOT simply the C type's range: CHIP reserves one encoding per type as
 * the "null" marker for nullable attributes — the minimum for signed
 * types, the maximum for unsigned ones (CHIP's NumericAttributeTraits,
 * src/app/util/attribute-storage-null-handling.h; mirrored by NullRaw()
 * in components/matter/src/endpoint_registry.cpp). Publishing that
 * encoding as a measurement would put a null on the wire and make a live
 * sensor look dead.
 *
 * The reserved value is excluded for every type, not only for attributes
 * actually flagged nullable. It costs one representable value at an
 * extreme that no sane physical range reaches, and it removes a class of
 * bug that only ever shows up on a saturated sensor in the field.
 *
 * @param type Attribute type. MCUHOME_ATTR_TYPE_BOOL is not a sensor
 *             channel type and is rejected.
 * @param min Filled with the lowest publishable value. Must not be NULL.
 * @param max Filled with the highest publishable value. Must not be NULL.
 *
 * @retval 0 @p min and @p max are set.
 * @retval -EINVAL @p min or @p max is NULL.
 * @retval -ENOTSUP @p type is MCUHOME_ATTR_TYPE_BOOL or unknown.
 */
int mcuhome_channel_value_range(enum mcuhome_attr_type type, int64_t *min, int64_t *max);

/**
 * @brief Convert a sensor reading to an attribute's raw units.
 *
 * Computes, in 64-bit integers:
 *
 *     raw = round(micro * num / (den * 1000000)) + offset
 *
 * rounded half away from zero, then clamped to
 * mcuhome_channel_value_range() for @p type.
 *
 * Rounding is symmetric on purpose: truncation would bias a temperature
 * series toward zero, which is visible as a systematic offset of up to
 * one LSB in opposite directions above and below 0 °C.
 *
 * @param micro Reading in the Zephyr sensor unit times 1e6 — exactly what
 *              sensor_value_to_micro() returns.
 * @param num Scale numerator.
 * @param den Scale denominator. Must not be 0.
 * @param offset Constant offset in raw units, applied after scaling.
 * @param type Attribute type, used for the clamp range.
 * @param out Filled with the raw value. Also filled (with the saturated
 *            value) when -ERANGE is returned, so a caller that treats
 *            saturation as "publish anyway, but say so" can do that. Must
 *            not be NULL.
 *
 * @retval 0 Converted exactly (rounding aside).
 * @retval -ERANGE The result did not fit the type and was clamped; @p out
 *                 holds the clamped value.
 * @retval -EINVAL @p den is 0 or @p out is NULL.
 * @retval -ENOTSUP @p type is not a sensor channel type.
 */
int mcuhome_channel_convert(int64_t micro, int32_t num, int32_t den, int32_t offset,
			    enum mcuhome_attr_type type, int64_t *out);

/**
 * @brief Decide whether a converted sample is worth publishing.
 *
 * @param raw Freshly converted value.
 * @param last Last published value. Ignored when @p has_last is false.
 * @param has_last Whether @p last holds a previously published value.
 *                 False after boot and after every fault episode, which
 *                 is what makes recovery always republish even when the
 *                 value has not moved.
 * @param delta Report threshold in raw units. 0 publishes every sample.
 *
 * @return true when @p raw should be published and reported.
 */
bool mcuhome_channel_needs_report(int64_t raw, int64_t last, bool has_last, uint32_t delta);

#ifdef __cplusplus
}
#endif

#endif /* MCUHOME_COMPONENTS_SENSOR_SENSOR_CONVERT_H_ */
