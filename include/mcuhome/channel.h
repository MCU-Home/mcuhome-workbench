/*
 * SPDX-FileCopyrightText: 2026 The MCUHome Contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * MCUHome channel layer — the produce side of the component model
 * (docs/design/component-model.md §2 and §5).
 *
 * A peripheral produces typed values; a *channel* binds one such value to
 * one Matter attribute: it publishes into the attribute's
 * `struct mcuhome_attr_store` cell (ADR 0014, <mcuhome/matter_tables.h>)
 * and triggers reporting via mcuhome_matter_attr_changed().
 *
 * Scope of contract v1 — deliberately narrow:
 *
 * - Read-only sensor channels only. Actuator/write-path semantics are an
 *   explicit open point of the component model (§9) and are not defined
 *   here; adding them must not change the meaning of anything below.
 * - Periodic sampling, report-on-delta. No triggers, no filters, no
 *   averaging. A channel samples every `sample_period_ms` and reports
 *   when the newly converted value differs from the last *reported* one
 *   by at least `report_delta`.
 * - Integer attribute types only (the four in `enum mcuhome_attr_type`
 *   that are not BOOL). A sensor reading has no meaningful mapping onto
 *   a boolean without a threshold, and thresholds are a component
 *   concern, not a channel one.
 *
 * THIS HEADER IS DUMB DATA ON PURPOSE. Both structs below are pure
 * description — no function pointers, no callbacks, no state. Today the
 * arrays are hand-written (samples/matter-node/src/main.c); tomorrow the
 * builder emits them from YAML exactly like it emits the Matter tables
 * (ADR 0014, builder-pipeline.md §1 "thin codegen + static tables"). Every
 * field must therefore be something a YAML-driven generator can compute
 * without embedding logic: constants, IDs, and integer scale factors —
 * never expressions, never code. Keep it that way.
 *
 * Runtime state (last reported value, fault latch, sampling countdown)
 * lives in the poller's own Kconfig-sized static pool
 * (components/sensor/src/sensor_poll.c), NOT in these structs, so the
 * generated arrays stay `const` and stay in flash.
 *
 * Unlike <mcuhome/matter_tables.h> this header does depend on Zephyr
 * (device model + sensor API): a channel's source is a Zephyr device by
 * definition of the component model ("peripherals are Zephyr devices",
 * §5). It still contains zero CHIP.
 */

#ifndef MCUHOME_CHANNEL_H_
#define MCUHOME_CHANNEL_H_

#include <stddef.h>
#include <stdint.h>

#include <zephyr/device.h>
#include <zephyr/drivers/sensor.h>

#include <mcuhome/matter_tables.h>

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief One typed value stream, bound to one Matter attribute.
 *
 * The "publish side" of a channel: where the value goes and when it is
 * worth telling a controller about it. Everything here is in the
 * attribute's own raw units — the unit the Matter specification defines
 * for that attribute (0.01 °C for TemperatureMeasurement::MeasuredValue,
 * 0.1 kPa for PressureMeasurement::MeasuredValue, …) — never in the
 * sensor's units. The conversion happens in the source binding
 * (@ref mcuhome_sensor_binding), which is the only place that knows both
 * unit systems.
 */
struct mcuhome_channel {
	/**
	 * RAM cell the value is published into. Must be the same cell the
	 * generated Matter tables point at for
	 * (`endpoint_id`, `cluster_id`, `attr_id`), and must never be NULL:
	 * a channel that cannot publish is a table bug, not a runtime
	 * condition.
	 */
	struct mcuhome_attr_store *store;
	/**
	 * Type of `store`'s payload. Must match the `type` of the same
	 * attribute in the Matter tables — the channel layer picks the
	 * mcuhome_attr_store_publish_*() helper from this field, and picking
	 * a different union member than the framework reads would be a
	 * silent value corruption. MCUHOME_ATTR_TYPE_BOOL is rejected at
	 * start (see the scope note at the top of this file).
	 */
	enum mcuhome_attr_type type;
	/**
	 * Endpoint of the bound attribute, for change notification.
	 *
	 * Together with `cluster_id`/`attr_id` below, this path and `store`
	 * must match the same attribute in the generated Matter tables
	 * (<mcuhome/matter_tables.h>) — today two independent statements of
	 * the same fact, until the builder emits both from one YAML source
	 * (ADR 0014). mcuhome_sensor_start() enforces the match at startup
	 * via mcuhome_matter_attr_store_lookup() (<mcuhome/matter.h>) and
	 * refuses to start on a mismatch rather than publish into the wrong
	 * attribute, or into one nobody reads.
	 */
	uint16_t endpoint_id;
	/** Cluster of the bound attribute, for change notification. */
	uint32_t cluster_id;
	/** ID of the bound attribute, for change notification. */
	uint32_t attr_id;
	/**
	 * Sampling period in milliseconds. Must be > 0.
	 *
	 * The poller runs one work item at the shortest period across all
	 * channels and counts each channel down from there, so a period that
	 * is not a whole multiple of that shortest period is effectively
	 * rounded UP to the next multiple. Uniform periods (the normal
	 * generated case) are exact.
	 */
	uint32_t sample_period_ms;
	/**
	 * Report threshold in the attribute's RAW units — the same units as
	 * `store`'s payload, not the sensor's.
	 *
	 * A converted sample is published (and reported) when it differs
	 * from the last published value by at least this much. `0` means
	 * "publish every sample", which is a legitimate but chatty choice —
	 * on a Thread SED it costs a radio wakeup per period.
	 *
	 * The first valid sample after boot, and the first valid sample
	 * after a sensor fault, always publish regardless of this value:
	 * until then the attribute reads as null on the wire and any value
	 * is news.
	 */
	uint32_t report_delta;
};

/**
 * @brief Binds one Zephyr sensor-API reading to one @ref mcuhome_channel.
 *
 * The "produce side": which device, which of its channels, and the
 * integer scale that turns Zephyr's unit into the attribute's raw unit.
 * This is the generic adapter the component model promises in §5 — "many
 * peripherals need zero own C code" — so a new sensor chip normally means
 * a new devicetree node plus a row here, and nothing else.
 *
 * Conversion, exactly:
 *
 *     micro = sensor_value_to_micro(&val)      // Zephyr unit * 1e6
 *     raw   = round(micro * scale_num / (scale_den * 1e6)) + offset
 *
 * evaluated in 64-bit integers, rounded half away from zero, then clamped
 * to the attribute type's range. Example, BMP180 pressure: the Zephyr
 * sensor API returns kPa, Matter's PressureMeasurement wants 0.1 kPa, so
 * `scale_num = 10`, `scale_den = 1`, `offset = 0`.
 *
 * `offset` is applied AFTER scaling and is therefore in the attribute's
 * raw units. It exists for unit systems with different zero points (a
 * fixed calibration trim, K vs °C, …), not as a general calibration
 * feature.
 */
struct mcuhome_sensor_binding {
	/** Source device. Normally `DEVICE_DT_GET(...)`. Never NULL. */
	const struct device *dev;
	/**
	 * Channel passed to sensor_sample_fetch_chan().
	 *
	 * Usually SENSOR_CHAN_ALL. It is a separate field from
	 * `zephyr_channel` because a good number of Zephyr drivers only
	 * implement the all-channels fetch and *assert* on anything else —
	 * Bosch's BMP180 is one of them
	 * (`__ASSERT_NO_MSG(chan == SENSOR_CHAN_ALL)` in
	 * drivers/sensor/bosch/bmp180/bmp180.c). Drivers where a narrower
	 * fetch saves a bus transaction can say so here.
	 *
	 * Bindings that share a `dev` AND are due in the same cycle fetch
	 * once, not once each: the poller reuses the first fetch's result.
	 * That is what makes a two-channel chip like the BMP180 cost one
	 * conversion per cycle instead of two.
	 */
	enum sensor_channel fetch_channel;
	/** Channel passed to sensor_channel_get(). */
	enum sensor_channel zephyr_channel;
	/** Scale numerator (Zephyr unit -> attribute raw unit). */
	int32_t scale_num;
	/** Scale denominator. Must not be 0. */
	int32_t scale_den;
	/** Constant offset, applied after scaling, in raw attribute units. */
	int32_t offset;
	/** Where the converted value goes. Never NULL. */
	const struct mcuhome_channel *channel;
};

/**
 * @brief Start periodic sampling of a static binding array.
 *
 * Validates @p bindings, then starts the poller: one k_work_delayable on
 * the channel layer's own workqueue, running at the shortest
 * `sample_period_ms` across the bound channels. Each cycle fetches every
 * due device once, converts each due binding's reading and publishes it
 * when `report_delta` says the change is worth reporting.
 *
 * Call this AFTER mcuhome_matter_start(): the first sample may publish
 * immediately and the reporting path must exist by then.
 *
 * A device that is not ready — the normal case when the sensor is simply
 * not attached — is NOT an error here. The call succeeds, the affected
 * attributes stay invalid (nullable attributes therefore read as null on
 * the wire) and the poller logs one warning per fault episode. A node
 * must boot, commission and stay commissioned without its sensors.
 *
 * @param bindings Static, immortal binding array. The poller keeps the
 *                 pointer; it must stay valid forever (`const` data in
 *                 flash is the intended case).
 * @param n        Number of entries. Must be > 0.
 *
 * @retval 0 The poller is running.
 * @retval -EINVAL @p bindings is NULL, @p n is 0, or an entry is
 *                 malformed (NULL device/channel/store, `scale_den == 0`,
 *                 `sample_period_ms == 0`).
 * @retval -ENOTSUP An entry binds a channel of type
 *                  MCUHOME_ATTR_TYPE_BOOL.
 * @retval -ENOSPC @p n exceeds CONFIG_MCUHOME_SENSOR_MAX_BINDINGS.
 * @retval -EALREADY The poller is already running. Contract v1 supports
 *                   exactly one binding array per node — the builder
 *                   emits one.
 */
int mcuhome_sensor_start(const struct mcuhome_sensor_binding *bindings, size_t n);

#ifdef __cplusplus
}
#endif

#endif /* MCUHOME_CHANNEL_H_ */
