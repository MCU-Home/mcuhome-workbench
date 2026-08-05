/*
 * SPDX-FileCopyrightText: 2026 The MCUHome Contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * Generic Zephyr-sensor channel adapter: the runtime behind
 * mcuhome_sensor_start() (<mcuhome/channel.h>).
 *
 * One k_work_delayable on the component's own workqueue drives every
 * sensor channel of the node. Per cycle it fetches each due device once,
 * converts each due binding's reading into the bound attribute's raw
 * units and publishes it when the channel's report delta says the change
 * is worth a Matter report.
 *
 * Design constraints this file is written against (AGENTS.md, and
 * component-model.md §5):
 *
 * - No heap. All state is a Kconfig-sized static pool; overflow is a hard
 *   error from mcuhome_sensor_start(), never a truncation.
 * - Workqueue context only, never an ISR: sensor_sample_fetch_chan() on
 *   an I2C sensor blocks — the BMP180's ultra-high-resolution mode sleeps
 *   ~26 ms inside the fetch — and Zephyr's sensor API is explicitly not
 *   ISR-safe.
 * - Own workqueue, not the system one, for exactly that reason: parking
 *   the system workqueue for tens of milliseconds per sample would show
 *   up as jitter in everything else that shares it, the networking stack
 *   included.
 * - Graceful degradation. A missing, unready or failing sensor
 *   invalidates its attribute (nullable attributes then read as null on
 *   the wire) and logs once. It never fails the node, never spins and
 *   never spams the log.
 */

#include <errno.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include <zephyr/device.h>
#include <zephyr/drivers/sensor.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/sys/util.h>

#include <mcuhome/channel.h>
#include <mcuhome/matter.h>
#include <mcuhome/matter_tables.h>

#include "sensor_convert.h"

LOG_MODULE_REGISTER(mcuhome_sensor, CONFIG_LOG_DEFAULT_LEVEL);

/**
 * Marker for "this binding was not due this cycle" in
 * binding_state::fetch_rc. Positive on purpose: sensor_sample_fetch_chan()
 * returns 0 or a negative errno, so no real fetch result can collide with
 * it.
 */
#define FETCH_NOT_DUE 1

/** Per-binding runtime state. Never in the (generated, const) tables. */
struct binding_state {
	/** Last value published to the attr store, in raw units. */
	int64_t last_raw;
	/** Milliseconds until this binding's next sample. */
	uint32_t due_ms;
	/** This cycle's fetch outcome, or FETCH_NOT_DUE. */
	int fetch_rc;
	/** Whether `last_raw` holds a published value. */
	bool has_last;
	/** Fault latch: the attribute is invalidated and was warned about. */
	bool faulted;
	/** Clamp latch: the last sample was out of range and was warned about. */
	bool clamped;
};

static struct binding_state states[CONFIG_MCUHOME_SENSOR_MAX_BINDINGS];

/** The registered binding array. NULL until mcuhome_sensor_start(). */
static const struct mcuhome_sensor_binding *bindings;
static size_t binding_count;

/** Poll cycle length: the shortest sample period across all channels. */
static uint32_t tick_ms;

static K_THREAD_STACK_DEFINE(sensor_workq_stack, CONFIG_MCUHOME_SENSOR_WORKQ_STACK_SIZE);
static struct k_work_q sensor_workq;

static void poll_work_handler(struct k_work *work);
static K_WORK_DELAYABLE_DEFINE(poll_work, poll_work_handler);

/* --- Fault handling ---------------------------------------------------- */

/**
 * Enter (or stay in) the degraded state for one binding.
 *
 * Edge-triggered on purpose — that is the rate limit. A sensor that is
 * simply not attached produces one warning for the lifetime of the boot,
 * not one per sample period; a sensor that flaps produces one line per
 * transition, which is the information a reader actually wants.
 *
 * Invalidating the store is what turns a dead sensor into a null on the
 * wire instead of a frozen last reading. Clearing `has_last` is what
 * makes the first good sample after recovery publish unconditionally,
 * even when the value has not moved by a full report delta.
 */
static void channel_fault(size_t index, int err, const char *what)
{
	const struct mcuhome_channel *ch = bindings[index].channel;
	struct binding_state *st = &states[index];

	st->has_last = false;

	if (st->faulted) {
		return;
	}
	st->faulted = true;

	mcuhome_attr_store_invalidate(ch->store);
	mcuhome_matter_attr_changed(ch->endpoint_id, ch->cluster_id, ch->attr_id);

	LOG_WRN("channel ep%u/0x%04x/0x%04x: %s failed (%d) - reporting null", ch->endpoint_id,
		(unsigned int)ch->cluster_id, (unsigned int)ch->attr_id, what, err);
}

static void channel_recovered(size_t index)
{
	const struct mcuhome_channel *ch = bindings[index].channel;
	struct binding_state *st = &states[index];

	if (!st->faulted) {
		return;
	}
	st->faulted = false;

	LOG_INF("channel ep%u/0x%04x/0x%04x: sensor recovered", ch->endpoint_id,
		(unsigned int)ch->cluster_id, (unsigned int)ch->attr_id);
}

/* --- Publication ------------------------------------------------------- */

/**
 * Store @p raw in the union member matching the channel's type.
 *
 * The value is already clamped to that type's publishable range by
 * mcuhome_channel_convert(), so every cast here is exact. Picking the
 * wrong member would not fail loudly — the framework reads whichever one
 * the *table* declares — which is why the type lives in the channel and
 * is validated against nothing else.
 */
static void channel_publish(const struct mcuhome_channel *ch, int64_t raw)
{
	switch (ch->type) {
	case MCUHOME_ATTR_TYPE_INT16S:
		mcuhome_attr_store_publish_i16s(ch->store, (int16_t)raw);
		break;
	case MCUHOME_ATTR_TYPE_INT16U:
		mcuhome_attr_store_publish_u16(ch->store, (uint16_t)raw);
		break;
	case MCUHOME_ATTR_TYPE_INT32S:
		mcuhome_attr_store_publish_i32s(ch->store, (int32_t)raw);
		break;
	case MCUHOME_ATTR_TYPE_INT32U:
		mcuhome_attr_store_publish_u32(ch->store, (uint32_t)raw);
		break;
	case MCUHOME_ATTR_TYPE_BOOL:
		/* Rejected by mcuhome_sensor_start(); unreachable. */
		break;
	}

	mcuhome_matter_attr_changed(ch->endpoint_id, ch->cluster_id, ch->attr_id);
}

/* --- Poll cycle -------------------------------------------------------- */

/**
 * Phase 1: advance every binding's countdown and fetch the due devices.
 *
 * Bindings that share a device and are due in the same cycle share one
 * fetch — the earlier binding performs it, the later ones inherit its
 * result. Without that, a two-channel chip like the BMP180 would run two
 * full conversions per cycle and the second one's temperature reading
 * would not even be the one its pressure compensation used.
 *
 * The scan is O(n^2) in the number of bindings. n is the number of sensor
 * channels on one node, bounded by CONFIG_MCUHOME_SENSOR_MAX_BINDINGS
 * (single digits); the alternative — requiring the generated array to be
 * grouped by device — would put a correctness constraint on the
 * generator that nothing else enforces.
 */
static void poll_fetch_phase(void)
{
	for (size_t i = 0; i < binding_count; i++) {
		const struct mcuhome_sensor_binding *b = &bindings[i];
		struct binding_state *st = &states[i];
		bool shared = false;

		if (st->due_ms > tick_ms) {
			st->due_ms -= tick_ms;
			st->fetch_rc = FETCH_NOT_DUE;
			continue;
		}
		st->due_ms = b->channel->sample_period_ms;

		if (!device_is_ready(b->dev)) {
			st->fetch_rc = -ENODEV;
			continue;
		}

		for (size_t j = 0; j < i; j++) {
			if (bindings[j].dev == b->dev && states[j].fetch_rc != FETCH_NOT_DUE) {
				st->fetch_rc = states[j].fetch_rc;
				shared = true;
				break;
			}
		}
		if (shared) {
			continue;
		}

		st->fetch_rc = sensor_sample_fetch_chan(b->dev, b->fetch_channel);
	}
}

/** Phase 2: read, convert and publish every binding that was fetched. */
static void poll_publish_phase(void)
{
	for (size_t i = 0; i < binding_count; i++) {
		const struct mcuhome_sensor_binding *b = &bindings[i];
		const struct mcuhome_channel *ch = b->channel;
		struct binding_state *st = &states[i];
		struct sensor_value val;
		int64_t raw;
		int rc;

		if (st->fetch_rc == FETCH_NOT_DUE) {
			continue;
		}

		if (st->fetch_rc != 0) {
			channel_fault(i, st->fetch_rc,
				      st->fetch_rc == -ENODEV ? "device not ready"
							      : "sample fetch");
			continue;
		}

		rc = sensor_channel_get(b->dev, b->zephyr_channel, &val);
		if (rc != 0) {
			channel_fault(i, rc, "channel get");
			continue;
		}

		rc = mcuhome_channel_convert(sensor_value_to_micro(&val), b->scale_num,
					     b->scale_den, b->offset, ch->type, &raw);
		if (rc == -ERANGE) {
			/* Out of the attribute's range: publish the clamped
			 * value rather than a null. A reading past the end of
			 * the scale is still information, and the Matter
			 * Min/Max attributes tell the controller where the
			 * scale ends. Deliberately NOT a fault — the value is
			 * usable and the next in-range sample must not be
			 * suppressed — but latched like one so a permanently
			 * saturated sensor warns once, not once per period. */
			if (!st->clamped) {
				st->clamped = true;
				LOG_WRN("channel ep%u/0x%04x/0x%04x: reading out of range, "
					"clamped to %d",
					ch->endpoint_id, (unsigned int)ch->cluster_id,
					(unsigned int)ch->attr_id, (int)raw);
			}
		} else if (rc != 0) {
			channel_fault(i, rc, "conversion");
			continue;
		} else {
			st->clamped = false;
		}

		channel_recovered(i);

		if (!mcuhome_channel_needs_report(raw, st->last_raw, st->has_last,
						  ch->report_delta)) {
			continue;
		}

		st->last_raw = raw;
		st->has_last = true;
		channel_publish(ch, raw);
	}
}

static void poll_work_handler(struct k_work *work)
{
	ARG_UNUSED(work);

	poll_fetch_phase();
	poll_publish_phase();

	k_work_schedule_for_queue(&sensor_workq, &poll_work, K_MSEC(tick_ms));
}

/* --- Start ------------------------------------------------------------- */

static int validate_bindings(const struct mcuhome_sensor_binding *b, size_t n)
{
	for (size_t i = 0; i < n; i++) {
		const struct mcuhome_channel *ch = b[i].channel;

		if (b[i].dev == NULL || ch == NULL || b[i].scale_den == 0) {
			LOG_ERR("binding %u: malformed", (unsigned int)i);
			return -EINVAL;
		}
		if (ch->store == NULL || ch->sample_period_ms == 0) {
			LOG_ERR("binding %u: malformed channel", (unsigned int)i);
			return -EINVAL;
		}
		if (ch->type == MCUHOME_ATTR_TYPE_BOOL) {
			LOG_ERR("binding %u: boolean channels are not sensor channels",
				(unsigned int)i);
			return -ENOTSUP;
		}
	}

	return 0;
}

int mcuhome_sensor_start(const struct mcuhome_sensor_binding *b, size_t n)
{
	static const struct k_work_queue_config workq_cfg = {
		.name = "mcuhome_sensor",
	};
	uint32_t shortest;
	int rc;

	if (bindings != NULL) {
		return -EALREADY;
	}
	if (b == NULL || n == 0) {
		return -EINVAL;
	}
	if (n > ARRAY_SIZE(states)) {
		LOG_ERR("%u bindings exceed CONFIG_MCUHOME_SENSOR_MAX_BINDINGS=%u", (unsigned int)n,
			(unsigned int)ARRAY_SIZE(states));
		return -ENOSPC;
	}

	rc = validate_bindings(b, n);
	if (rc != 0) {
		return rc;
	}

	shortest = b[0].channel->sample_period_ms;
	for (size_t i = 1; i < n; i++) {
		shortest = MIN(shortest, b[i].channel->sample_period_ms);
	}

	for (size_t i = 0; i < n; i++) {
		states[i] = (struct binding_state){
			.due_ms = 0, /* every binding samples on the first cycle */
			.fetch_rc = FETCH_NOT_DUE,
		};

		/* A sensor that is not attached is a supported state, not a
		 * failure: the node must boot and commission without it. Say
		 * so once here so the reason is in the log before the first
		 * poll cycle turns it into a null-reporting attribute. */
		if (!device_is_ready(b[i].dev)) {
			LOG_WRN("binding %u: %s not ready - its attribute will report null",
				(unsigned int)i, b[i].dev->name);
		}
	}

	bindings = b;
	binding_count = n;
	tick_ms = shortest;

	k_work_queue_start(&sensor_workq, sensor_workq_stack,
			   K_THREAD_STACK_SIZEOF(sensor_workq_stack),
			   CONFIG_MCUHOME_SENSOR_WORKQ_PRIO, &workq_cfg);
	k_work_schedule_for_queue(&sensor_workq, &poll_work, K_NO_WAIT);

	LOG_INF("%u sensor channel(s) polling every %u ms", (unsigned int)n, (unsigned int)tick_ms);

	return 0;
}
