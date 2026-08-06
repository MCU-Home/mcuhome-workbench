/*
 * SPDX-FileCopyrightText: 2026 The MCUHome Contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * DRBG and state machine behind the MCUHome entropy driver. See
 * entropy_ipc_core.h for the split between this layer and the transport.
 */

#include "entropy_ipc_core.h"

#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/sys/util.h>

#include <errno.h>
#include <limits.h>
#include <string.h>

LOG_MODULE_REGISTER(mcuhome_entropy, MCUHOME_ENTROPY_LOG_LEVEL);

/* NIST SP 800-90A personalization string. Adds no entropy; it keeps two
 * instances that were somehow handed identical seed material from
 * producing identical streams, and costs one static string.
 */
static const unsigned char drbg_personalization[] = "mcuhome-entropy-ipc-v1";

/* How long a request counts as outstanding, and equivalently how often a
 * caller blocked on the first seed re-checks. The two are the same
 * number on purpose: a request that has gone unanswered for this long is
 * assumed lost — the peer may not have been listening yet — and the next
 * iteration re-issues it. Without an expiry a single dropped request
 * would wedge the driver forever, because nothing else ever clears the
 * in-flight flag. Short enough for several retries inside the seed
 * timeout, long enough not to flood the IPC channel.
 */
#define SEED_REQUEST_TIMEOUT_MS 250

/* mbedtls_ctr_drbg_random() refuses more than this per call. */
#define DRBG_MAX_REQUEST MBEDTLS_CTR_DRBG_MAX_REQUEST

/*
 * Entropy callback handed to mbedTLS.
 *
 * Serves the bytes already fetched from the provider — the DRBG must
 * never reach out to the transport itself, because it is called with the
 * core spinlock held during a reseed.
 */
static int core_entropy_cb(void *ctx, unsigned char *out, size_t len)
{
	struct mcuhome_entropy_core *core = ctx;

	if (len > (size_t)(core->feed_len - core->feed_off)) {
		return MBEDTLS_ERR_CTR_DRBG_ENTROPY_SOURCE_FAILED;
	}

	memcpy(out, &core->feed[core->feed_off], len);
	core->feed_off += (uint16_t)len;

	return 0;
}

/*
 * Move a staged response into the feed buffer.
 *
 * Returns true when a complete seed is ready to be consumed. Clears the
 * staging buffer either way, so a partial delivery does not linger.
 */
static bool take_staged(struct mcuhome_entropy_core *core)
{
	k_spinlock_key_t key = k_spin_lock(&core->lock);
	bool complete = (core->staged_len == MCUHOME_ENTROPY_SEED_BYTES);

	if (complete) {
		memcpy(core->feed, core->staged, MCUHOME_ENTROPY_SEED_BYTES);
		core->feed_len = MCUHOME_ENTROPY_SEED_BYTES;
		core->feed_off = 0U;
	}

	memset(core->staged, 0, sizeof(core->staged));
	core->staged_len = 0U;
	core->request_in_flight = false;

	k_spin_unlock(&core->lock, key);

	return complete;
}

static bool staged_available(struct mcuhome_entropy_core *core)
{
	k_spinlock_key_t key = k_spin_lock(&core->lock);
	bool available = (core->staged_len > 0U);

	k_spin_unlock(&core->lock, key);

	return available;
}

/* Ask the provider for a seed unless a request is already outstanding
 * and still within its expiry.
 */
static int request_seed(struct mcuhome_entropy_core *core)
{
	int64_t now = k_uptime_get();
	k_spinlock_key_t key = k_spin_lock(&core->lock);
	bool busy = core->request_in_flight && now < core->request_expiry;

	if (!busy) {
		core->request_in_flight = true;
		core->request_expiry = now + SEED_REQUEST_TIMEOUT_MS;
	}
	k_spin_unlock(&core->lock, key);

	if (busy) {
		return 0;
	}

	int err = core->cfg.src->request(core->cfg.src_ctx, MCUHOME_ENTROPY_SEED_BYTES);

	if (err < 0) {
		mcuhome_entropy_core_request_failed(core);
	}

	return err;
}

static void schedule_next_reseed(struct mcuhome_entropy_core *core)
{
	core->next_reseed_uptime =
		(core->cfg.reseed_interval_s == 0U)
			? INT64_MAX
			: k_uptime_get() + (int64_t)core->cfg.reseed_interval_s * MSEC_PER_SEC;
}

/*
 * Instantiate the DRBG from the staged seed. Thread context only.
 *
 * Deliberately not run under the spinlock: while unseeded, no other path
 * may touch the DRBG, and mbedtls_ctr_drbg_seed() is the most expensive
 * block-cipher sequence in this driver. Mutual exclusion against a
 * second thread racing for the same first seed comes from install_lock —
 * calling mbedtls_ctr_drbg_seed() twice on one context is a contract
 * violation, not merely a data race.
 */
static int install_seed(struct mcuhome_entropy_core *core)
{
	int err = 0;

	k_mutex_lock(&core->install_lock, K_FOREVER);

	if (core->seeded) {
		goto out;
	}

	if (!take_staged(core)) {
		err = -EAGAIN;
		goto out;
	}

	int ret = mbedtls_ctr_drbg_seed(&core->drbg, core_entropy_cb, core, drbg_personalization,
					sizeof(drbg_personalization) - 1U);

	if (ret != 0) {
		LOG_ERR("CTR-DRBG instantiation failed (mbedtls %d)", ret);
		err = -EIO;
		goto out;
	}

	/* This driver owns the reseed policy (wall-clock interval below), so
	 * the library's own request counter must never fire: it would call
	 * the entropy callback at an unpredictable moment, when the feed
	 * buffer is empty, and turn an ordinary psa_generate_random() into a
	 * hard failure in the middle of a Matter handshake.
	 */
	mbedtls_ctr_drbg_set_reseed_interval(&core->drbg, INT_MAX);

	k_spinlock_key_t key = k_spin_lock(&core->lock);

	core->seeded = true;
	memset(core->feed, 0, sizeof(core->feed));
	core->feed_len = 0U;
	core->feed_off = 0U;
	k_spin_unlock(&core->lock, key);

	schedule_next_reseed(core);
	LOG_INF("CTR-DRBG seeded from %s", core->cfg.src->name);

out:
	k_mutex_unlock(&core->install_lock);
	return err;
}

/*
 * Fold a staged response into a running DRBG. Thread context only.
 *
 * Unlike instantiation this does run under the spinlock: the DRBG is in
 * service, so an ISR generating concurrently would corrupt its state.
 * The cost is one block-cipher derivation with interrupts masked, once
 * per reseed interval.
 */
static void reseed(struct mcuhome_entropy_core *core)
{
	k_mutex_lock(&core->install_lock, K_FOREVER);

	if (!take_staged(core)) {
		k_mutex_unlock(&core->install_lock);
		return;
	}

	k_spinlock_key_t key = k_spin_lock(&core->lock);
	int ret = mbedtls_ctr_drbg_reseed(&core->drbg, NULL, 0);

	memset(core->feed, 0, sizeof(core->feed));
	core->feed_len = 0U;
	core->feed_off = 0U;
	k_spin_unlock(&core->lock, key);

	if (ret != 0) {
		/* Keep serving. A CTR-DRBG that cannot reseed is still a
		 * CSPRNG under SP 800-90A as long as its state stayed
		 * secret; refusing randomness here would take down a node
		 * whose actual problem is a wedged network core, and that
		 * failure is already visible from the radio side.
		 */
		LOG_ERR("CTR-DRBG reseed failed (mbedtls %d), continuing on the previous state",
			ret);
	} else {
		LOG_DBG("CTR-DRBG reseeded from %s", core->cfg.src->name);
	}

	schedule_next_reseed(core);
	k_mutex_unlock(&core->install_lock);
}

/* Generate under the spinlock. Callable from thread and ISR context. */
static int generate(struct mcuhome_entropy_core *core, uint8_t *buffer, uint16_t length)
{
	k_spinlock_key_t key = k_spin_lock(&core->lock);
	int ret = 0;

	if (!core->seeded) {
		k_spin_unlock(&core->lock, key);
		return -EIO;
	}

	while (length > 0U) {
		size_t chunk = MIN((size_t)length, (size_t)DRBG_MAX_REQUEST);

		ret = mbedtls_ctr_drbg_random(&core->drbg, buffer, chunk);
		if (ret != 0) {
			break;
		}

		buffer += chunk;
		length -= (uint16_t)chunk;
	}

	k_spin_unlock(&core->lock, key);

	if (ret != 0) {
		LOG_ERR("CTR-DRBG generate failed (mbedtls %d)", ret);
		return -EIO;
	}

	return 0;
}

void mcuhome_entropy_core_init(struct mcuhome_entropy_core *core,
			       const struct mcuhome_entropy_core_cfg *cfg)
{
	memset(core, 0, sizeof(*core));
	core->cfg = *cfg;

	k_mutex_init(&core->install_lock);
	k_sem_init(&core->arrival, 0, 1);
	mbedtls_ctr_drbg_init(&core->drbg);

	core->next_reseed_uptime = INT64_MAX;
}

int mcuhome_entropy_core_start(struct mcuhome_entropy_core *core)
{
	return request_seed(core);
}

int mcuhome_entropy_core_stage(struct mcuhome_entropy_core *core, const uint8_t *buf, size_t len)
{
	if (len != MCUHOME_ENTROPY_SEED_BYTES) {
		LOG_ERR("%s returned %zu bytes, expected %zu", core->cfg.src->name, len,
			MCUHOME_ENTROPY_SEED_BYTES);
		mcuhome_entropy_core_request_failed(core);
		return -EINVAL;
	}

	k_spinlock_key_t key = k_spin_lock(&core->lock);

	memcpy(core->staged, buf, MCUHOME_ENTROPY_SEED_BYTES);
	core->staged_len = MCUHOME_ENTROPY_SEED_BYTES;
	k_spin_unlock(&core->lock, key);

	k_sem_give(&core->arrival);

	return 0;
}

void mcuhome_entropy_core_request_failed(struct mcuhome_entropy_core *core)
{
	k_spinlock_key_t key = k_spin_lock(&core->lock);

	core->request_in_flight = false;
	k_spin_unlock(&core->lock, key);
}

bool mcuhome_entropy_core_is_seeded(struct mcuhome_entropy_core *core)
{
	k_spinlock_key_t key = k_spin_lock(&core->lock);
	bool seeded = core->seeded;

	k_spin_unlock(&core->lock, key);

	return seeded;
}

int mcuhome_entropy_core_get(struct mcuhome_entropy_core *core, uint8_t *buffer, uint16_t length)
{
	if (buffer == NULL || length == 0U) {
		return -EINVAL;
	}

	/* entropy_get_entropy() is documented as a thread API that may
	 * block, but sys_rand_get() routes through it from any context.
	 * Where waiting is impossible, degrade to the ISR contract instead
	 * of asserting: an unseeded answer is a clean -EIO, and callers
	 * that must not fail (sys_csrand_get) propagate it.
	 */
	if (k_is_in_isr() || k_is_pre_kernel()) {
		return mcuhome_entropy_core_is_seeded(core) ? generate(core, buffer, length) : -EIO;
	}

	if (!mcuhome_entropy_core_is_seeded(core)) {
		int64_t deadline =
			k_uptime_get() + (int64_t)core->cfg.seed_timeout_s * MSEC_PER_SEC;

		do {
			int rerr = request_seed(core);

			/* The transport's connect phase has not run yet:
			 * this caller is an earlier POST_KERNEL init level,
			 * and waiting here would stall the very init thread
			 * that brings the transport up. Fail fast; the
			 * caller's fallback handles it (and would have after
			 * the timeout anyway).
			 */
			if (rerr == -ENODEV) {
				LOG_WRN("entropy requested before the %s transport is up "
					"(boot-time init order) — failing fast",
					core->cfg.src->name);
				return -EIO;
			}

			if (k_sem_take(&core->arrival, K_MSEC(SEED_REQUEST_TIMEOUT_MS)) == 0) {
				(void)install_seed(core);
			}
		} while (!mcuhome_entropy_core_is_seeded(core) && k_uptime_get() < deadline);

		if (!mcuhome_entropy_core_is_seeded(core)) {
			LOG_ERR("no seed from %s within %u s — refusing to hand out randomness",
				core->cfg.src->name, core->cfg.seed_timeout_s);
			return -EIO;
		}
	}

	/* Reseed bookkeeping, thread context only: consume whatever the
	 * provider has already delivered, otherwise ask once the interval
	 * has elapsed and let a later call fold the answer in.
	 */
	if (staged_available(core)) {
		reseed(core);
	} else if (k_uptime_get() >= core->next_reseed_uptime) {
		(void)request_seed(core);
	}

	return generate(core, buffer, length);
}

int mcuhome_entropy_core_get_isr(struct mcuhome_entropy_core *core, uint8_t *buffer,
				 uint16_t length, uint32_t flags)
{
	/* ENTROPY_BUSYWAIT is meaningless here: there is no hardware to
	 * spin on, only a peer core that cannot be waited for from an ISR.
	 * Either the DRBG is seeded and the answer is immediate, or it is
	 * not and no amount of busy-waiting would change that.
	 */
	ARG_UNUSED(flags);

	if (buffer == NULL || length == 0U) {
		return -EINVAL;
	}

	if (!mcuhome_entropy_core_is_seeded(core)) {
		return -ENOTSUP;
	}

	int err = generate(core, buffer, length);

	return (err == 0) ? (int)length : err;
}
