/*
 * SPDX-FileCopyrightText: 2026 The MCUHome Contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * Host tests for the entropy driver's DRBG and state machine
 * (drivers/entropy/entropy_ipc_core.c) and for the wire framing both
 * cores share (include/mcuhome/entropy_ipc.h).
 *
 * The transport is faked: a seed source that records requests and hands
 * back bytes on command. That is the whole point of the seam — every
 * state transition the driver can make on target (unseeded, waiting,
 * seeded, reseeding, provider gone) is reachable here without an IPC
 * instance, a second core, or a debugger.
 */

#include <zephyr/kernel.h>
#include <zephyr/ztest.h>

#include <mcuhome/entropy_ipc.h>

#include "entropy_ipc_core.h"

#include <string.h>

/* --------------------------------------------------------------------
 * Fake seed provider.
 * ----------------------------------------------------------------- */

struct fake_source {
	struct mcuhome_entropy_core *core;
	unsigned int requests;
	uint16_t last_nbytes;
	/* When true, request() answers immediately, the way a local TRNG
	 * provider would. When false it accepts and stays silent, the way
	 * the IPC provider does while the peer is absent.
	 */
	bool auto_answer;
	/* Forced failure of the request itself (transport not bound). */
	int request_result;
	/* Byte pattern handed back, incremented per answer so successive
	 * seeds differ.
	 */
	uint8_t pattern;
};

static struct fake_source fake;
static struct mcuhome_entropy_core core;

static void fake_answer(uint16_t nbytes)
{
	uint8_t buf[MCUHOME_ENTROPY_IPC_MAX_BYTES];

	zassert_true(nbytes <= sizeof(buf), "provider asked for too much");
	memset(buf, fake.pattern++, nbytes);
	zassert_ok(mcuhome_entropy_core_stage(fake.core, buf, nbytes));
}

static int fake_request(void *ctx, uint16_t nbytes)
{
	struct fake_source *src = ctx;

	src->requests++;
	src->last_nbytes = nbytes;

	if (src->request_result != 0) {
		return src->request_result;
	}

	if (src->auto_answer) {
		fake_answer(nbytes);
	}

	return 0;
}

static const struct mcuhome_entropy_seed_source fake_seed_source = {
	.name = "fake",
	.request = fake_request,
};

static void setup_core(uint32_t reseed_interval_s, uint32_t seed_timeout_s, bool auto_answer)
{
	const struct mcuhome_entropy_core_cfg cfg = {
		.src = &fake_seed_source,
		.src_ctx = &fake,
		.reseed_interval_s = reseed_interval_s,
		.seed_timeout_s = seed_timeout_s,
	};

	memset(&fake, 0, sizeof(fake));
	fake.core = &core;
	fake.auto_answer = auto_answer;
	fake.pattern = 1U;

	mcuhome_entropy_core_init(&core, &cfg);
}

static bool all_zero(const uint8_t *buf, size_t len)
{
	for (size_t i = 0; i < len; i++) {
		if (buf[i] != 0U) {
			return false;
		}
	}

	return true;
}

/* --------------------------------------------------------------------
 * Wire framing.
 * ----------------------------------------------------------------- */

ZTEST(entropy_ipc, test_magics_carry_the_version)
{
	/* Both magics must encode the same protocol version in their low
	 * byte and must not be confusable with each other — that is what
	 * lets a stale image be recognised on the first message.
	 */
	zassert_equal(MCUHOME_ENTROPY_IPC_MAGIC_REQ & 0xffU, MCUHOME_ENTROPY_IPC_VERSION);
	zassert_equal(MCUHOME_ENTROPY_IPC_MAGIC_RSP & 0xffU, MCUHOME_ENTROPY_IPC_VERSION);
	zassert_not_equal(MCUHOME_ENTROPY_IPC_MAGIC_REQ, MCUHOME_ENTROPY_IPC_MAGIC_RSP);
}

ZTEST(entropy_ipc, test_response_length_arithmetic)
{
	struct mcuhome_entropy_ipc_rsp rsp;

	/* An empty response is header-only, and the payload follows the
	 * header with no padding — both cores compute the send length from
	 * this macro, so a struct-layout change must show up here.
	 */
	zassert_equal(MCUHOME_ENTROPY_IPC_RSP_ERR_LEN, MCUHOME_ENTROPY_IPC_RSP_LEN(0));
	zassert_equal(MCUHOME_ENTROPY_IPC_RSP_LEN(0), 4U);
	zassert_equal(MCUHOME_ENTROPY_IPC_RSP_LEN(MCUHOME_ENTROPY_IPC_MAX_BYTES), sizeof(rsp));

	for (uint16_t n = 0; n <= MCUHOME_ENTROPY_IPC_MAX_BYTES; n++) {
		zassert_equal(MCUHOME_ENTROPY_IPC_RSP_LEN(n),
			      MCUHOME_ENTROPY_IPC_RSP_LEN(0) + (size_t)n);
	}
}

ZTEST(entropy_ipc, test_seed_fits_one_message)
{
	/* The driver fetches a full CTR-DRBG instantiation in one exchange;
	 * if the DRBG configuration ever outgrows the message this must
	 * fail here rather than on target.
	 */
	zassert_true(MCUHOME_ENTROPY_SEED_BYTES <= MCUHOME_ENTROPY_IPC_MAX_BYTES);
	zassert_true(MCUHOME_ENTROPY_SEED_BYTES >= 32U);
}

/* --------------------------------------------------------------------
 * State machine.
 * ----------------------------------------------------------------- */

ZTEST(entropy_ipc, test_unseeded_after_init)
{
	uint8_t buf[16];

	setup_core(300U, 1U, false);

	zassert_false(mcuhome_entropy_core_is_seeded(&core));

	/* Nothing has been requested yet: init must not touch the transport. */
	zassert_equal(fake.requests, 0U);

	/* The ISR path never waits, so it refuses immediately. */
	zassert_equal(mcuhome_entropy_core_get_isr(&core, buf, sizeof(buf), 0), -ENOTSUP);
}

ZTEST(entropy_ipc, test_start_requests_a_full_seed)
{
	setup_core(300U, 1U, false);

	zassert_ok(mcuhome_entropy_core_start(&core));
	zassert_equal(fake.requests, 1U);
	zassert_equal(fake.last_nbytes, MCUHOME_ENTROPY_SEED_BYTES);

	/* One request at a time: a second start must not put another on the
	 * wire while the first is outstanding.
	 */
	zassert_ok(mcuhome_entropy_core_start(&core));
	zassert_equal(fake.requests, 1U);
}

ZTEST(entropy_ipc, test_timeout_fails_loud)
{
	uint8_t buf[32];
	int64_t start;
	int err;

	/* Provider accepts requests and never answers — a network core that
	 * is not running the entropy service.
	 */
	setup_core(300U, 1U, false);

	memset(buf, 0, sizeof(buf));
	start = k_uptime_get();
	err = mcuhome_entropy_core_get(&core, buf, sizeof(buf));

	zassert_equal(err, -EIO, "must fail loud, got %d", err);
	zassert_false(mcuhome_entropy_core_is_seeded(&core));

	/* It really waited, and it really gave up. */
	zassert_true(k_uptime_get() - start >= 1000, "returned before the timeout elapsed");
	zassert_true(k_uptime_get() - start < 5000, "waited far past the timeout");

	/* And it retried inside the window rather than asking once. */
	zassert_true(fake.requests > 1U, "expected retries, saw %u", fake.requests);

	/* Nothing was written into the caller's buffer. */
	zassert_true(all_zero(buf, sizeof(buf)), "buffer touched on failure");
}

/* A provider that answers -ENODEV signals "the transport's connect
 * phase has not run yet": the caller is an earlier boot-time init level,
 * and dwelling in the seed timeout would stall the single init thread
 * the connect phase itself needs (observed live: secure_storage and
 * OpenThread settings init serialized the whole boot behind 7 x 10 s
 * timeouts). The core must fail immediately, not wait.
 */
ZTEST(entropy_ipc, test_preconnect_fails_fast)
{
	uint8_t buf[32];
	int64_t start;
	int err;

	setup_core(300U, 5U, false);
	fake.request_result = -ENODEV;

	memset(buf, 0, sizeof(buf));
	start = k_uptime_get();
	err = mcuhome_entropy_core_get(&core, buf, sizeof(buf));

	zassert_equal(err, -EIO, "must fail, got %d", err);
	/* Immediately — nowhere near the 5 s seed timeout. */
	zassert_true(k_uptime_get() - start < 500, "dwelled despite -ENODEV");
	zassert_false(mcuhome_entropy_core_is_seeded(&core));
	zassert_true(all_zero(buf, sizeof(buf)), "buffer touched on failure");
}

ZTEST(entropy_ipc, test_request_failure_is_retried)
{
	uint8_t buf[8];

	/* Transport refuses outright (endpoint not bound yet). */
	setup_core(300U, 1U, false);
	fake.request_result = -ENOTCONN;

	zassert_equal(mcuhome_entropy_core_get(&core, buf, sizeof(buf)), -EIO);

	/* A rejected request must not be counted as outstanding, otherwise
	 * the driver would sit on a request that never happened.
	 */
	zassert_true(fake.requests > 1U, "a failed request was never re-issued");
}

ZTEST(entropy_ipc, test_seeds_and_serves)
{
	uint8_t a[64];
	uint8_t b[64];

	setup_core(300U, 5U, true);

	zassert_ok(mcuhome_entropy_core_get(&core, a, sizeof(a)));
	zassert_true(mcuhome_entropy_core_is_seeded(&core));

	zassert_ok(mcuhome_entropy_core_get(&core, b, sizeof(b)));

	zassert_false(all_zero(a, sizeof(a)));
	zassert_false(all_zero(b, sizeof(b)));
	zassert_true(memcmp(a, b, sizeof(a)) != 0, "DRBG repeated its output");
}

ZTEST(entropy_ipc, test_seeded_serves_from_isr_path)
{
	uint8_t buf[24];
	int ret;

	setup_core(300U, 5U, true);
	zassert_ok(mcuhome_entropy_core_get(&core, buf, sizeof(buf)));

	memset(buf, 0, sizeof(buf));
	ret = mcuhome_entropy_core_get_isr(&core, buf, sizeof(buf), 0);

	/* The ISR API returns the byte count, not 0. */
	zassert_equal(ret, (int)sizeof(buf), "expected %zu bytes, got %d", sizeof(buf), ret);
	zassert_false(all_zero(buf, sizeof(buf)));
}

ZTEST(entropy_ipc, test_no_further_traffic_once_seeded)
{
	uint8_t buf[16];
	unsigned int after_seed;

	/* Reseeding disabled: once seeded, the provider must be left alone. */
	setup_core(0U, 5U, true);

	zassert_ok(mcuhome_entropy_core_get(&core, buf, sizeof(buf)));
	after_seed = fake.requests;

	for (int i = 0; i < 50; i++) {
		zassert_ok(mcuhome_entropy_core_get(&core, buf, sizeof(buf)));
	}

	zassert_equal(fake.requests, after_seed,
		      "the DRBG went back to the provider %u extra times",
		      fake.requests - after_seed);
}

ZTEST(entropy_ipc, test_reseed_interval_triggers_a_request)
{
	uint8_t buf[16];
	unsigned int after_seed;

	/* Shortest interval that is still a whole second. The provider does
	 * not auto-answer, so the reseed request is observable on its own
	 * before any response folds it in.
	 */
	setup_core(1U, 5U, false);

	zassert_ok(mcuhome_entropy_core_start(&core));
	fake_answer(fake.last_nbytes);
	zassert_ok(mcuhome_entropy_core_get(&core, buf, sizeof(buf)));
	zassert_true(mcuhome_entropy_core_is_seeded(&core));

	after_seed = fake.requests;

	/* Before the interval elapses: no traffic. */
	zassert_ok(mcuhome_entropy_core_get(&core, buf, sizeof(buf)));
	zassert_equal(fake.requests, after_seed);

	k_sleep(K_MSEC(1100));

	/* After it: exactly one new request, issued without blocking. */
	zassert_ok(mcuhome_entropy_core_get(&core, buf, sizeof(buf)));
	zassert_equal(fake.requests, after_seed + 1U);
	zassert_equal(fake.last_nbytes, MCUHOME_ENTROPY_SEED_BYTES);

	/* The answer is folded in on a later call, and serving keeps
	 * working across the reseed.
	 */
	fake_answer(fake.last_nbytes);
	zassert_ok(mcuhome_entropy_core_get(&core, buf, sizeof(buf)));
	zassert_false(all_zero(buf, sizeof(buf)));

	/* Consuming the response resets the interval — no request storm. */
	after_seed = fake.requests;
	zassert_ok(mcuhome_entropy_core_get(&core, buf, sizeof(buf)));
	zassert_equal(fake.requests, after_seed);
}

ZTEST(entropy_ipc, test_short_delivery_rejected)
{
	uint8_t partial[MCUHOME_ENTROPY_SEED_BYTES - 1U] = {0};
	uint8_t buf[16];

	setup_core(300U, 1U, false);

	/* A truncated response must never be treated as a seed. */
	zassert_equal(mcuhome_entropy_core_stage(&core, partial, sizeof(partial)), -EINVAL);
	zassert_false(mcuhome_entropy_core_is_seeded(&core));
	zassert_equal(mcuhome_entropy_core_get(&core, buf, sizeof(buf)), -EIO);
}

ZTEST(entropy_ipc, test_rejects_empty_requests)
{
	uint8_t buf[8];

	setup_core(300U, 5U, true);
	zassert_ok(mcuhome_entropy_core_get(&core, buf, sizeof(buf)));

	zassert_equal(mcuhome_entropy_core_get(&core, NULL, 8U), -EINVAL);
	zassert_equal(mcuhome_entropy_core_get(&core, buf, 0U), -EINVAL);
	zassert_equal(mcuhome_entropy_core_get_isr(&core, NULL, 8U, 0), -EINVAL);
	zassert_equal(mcuhome_entropy_core_get_isr(&core, buf, 0U, 0), -EINVAL);
}

ZTEST(entropy_ipc, test_request_larger_than_drbg_max)
{
	/* mbedtls_ctr_drbg_random() caps a single call; the driver has to
	 * chunk around that rather than return a short read.
	 */
	static uint8_t buf[MBEDTLS_CTR_DRBG_MAX_REQUEST + 64U];

	setup_core(0U, 5U, true);

	memset(buf, 0, sizeof(buf));
	zassert_ok(mcuhome_entropy_core_get(&core, buf, sizeof(buf)));
	zassert_false(all_zero(&buf[MBEDTLS_CTR_DRBG_MAX_REQUEST], 64U),
		      "the tail past MBEDTLS_CTR_DRBG_MAX_REQUEST was left unwritten");
}

ZTEST_SUITE(entropy_ipc, NULL, NULL, NULL, NULL, NULL);
