/*
 * SPDX-FileCopyrightText: 2026 The MCUHome Contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * DRBG and state machine behind the MCUHome entropy driver.
 *
 * Internal header: this is not public API, it exists so the part of the
 * driver that has no transport dependency can be built and exercised on
 * the host (tests/entropy_ipc/). Everything transport-specific — the
 * ipc_service endpoint, the devicetree node, the SYS_INIT — lives in
 * entropy_mcuhome_ipc.c and reaches this layer through
 * @ref mcuhome_entropy_seed_source.
 */

#ifndef MCUHOME_DRIVERS_ENTROPY_IPC_CORE_H_
#define MCUHOME_DRIVERS_ENTROPY_IPC_CORE_H_

#include <zephyr/kernel.h>
#include <zephyr/sys/atomic.h>

#include <mbedtls/private/ctr_drbg.h>

#include <mcuhome/entropy_ipc.h>

/* CTR-DRBG has two back ends: the low-level AES module when MBEDTLS_AES_C
 * is defined, and the PSA cipher API when it is not. Only the first is
 * usable here — this code is what PSA gets its randomness from, so
 * calling into PSA to produce it inverts the dependency, and on top of
 * that a reseed runs with a spinlock held.
 *
 * MBEDTLS_AES_C is derived from PSA_WANT_KEY_TYPE_AES, but only inside a
 * build that implements PSA itself: the deriving header
 * (crypto_adjust_config_enable_builtins.h) is included from
 * tf-psa-crypto/build_info.h under `#if defined(MBEDTLS_PSA_CRYPTO_C)`.
 * Hence both Kconfig symbols, and this guard — without it the failure is
 * an implicit-declaration error deep inside ctr_drbg.c that says nothing
 * about the cause.
 */
#if !defined(MBEDTLS_AES_C)
#error "mcuhome,entropy-ipc needs MBEDTLS_AES_C: enable CONFIG_MBEDTLS_PSA_CRYPTO_C and CONFIG_PSA_WANT_KEY_TYPE_AES"
#endif

#ifdef __cplusplus
extern "C" {
#endif

/* Log at the entropy subsystem's level when this code is compiled as
 * part of a driver build. The host test suite builds it standalone,
 * without CONFIG_ENTROPY_GENERATOR and therefore without that symbol.
 */
#if defined(CONFIG_ENTROPY_LOG_LEVEL)
#define MCUHOME_ENTROPY_LOG_LEVEL CONFIG_ENTROPY_LOG_LEVEL
#else
#define MCUHOME_ENTROPY_LOG_LEVEL LOG_LEVEL_INF
#endif

/* CTR-DRBG instantiation consumes an entropy input plus a nonce; a
 * reseed consumes only the entropy input. Both are fetched in a single
 * exchange, so the seed request is sized for the larger of the two.
 * With the default 256-bit strength this is 32 + 16 bytes.
 */
#define MCUHOME_ENTROPY_SEED_BYTES                                                                 \
	((size_t)(MBEDTLS_CTR_DRBG_ENTROPY_LEN + MBEDTLS_CTR_DRBG_ENTROPY_NONCE_LEN))

BUILD_ASSERT(MCUHOME_ENTROPY_SEED_BYTES <= MCUHOME_ENTROPY_IPC_MAX_BYTES,
	     "CTR-DRBG seed does not fit in one entropy IPC message");

/**
 * @brief Where seed material comes from.
 *
 * The seam that keeps this layer independent of how the bytes are
 * obtained. The shipped implementation is the network-core IPC service
 * (entropy_mcuhome_ipc.c); a build that gains a local TRNG — on Nordic
 * silicon by way of the `nrf_cc3xx` blob, subject to ADR 0013 — replaces
 * this structure and nothing else.
 *
 * @c request is asynchronous by contract: it must not block, and it must
 * not deliver the bytes itself. The provider calls
 * mcuhome_entropy_core_stage() once they have arrived. A provider that
 * *can* produce bytes synchronously (a local TRNG) simply calls
 * mcuhome_entropy_core_stage() from inside @c request.
 */
struct mcuhome_entropy_seed_source {
	/** Human-readable provider name, used in log messages. */
	const char *name;
	/**
	 * @brief Ask for @p nbytes fresh entropy bytes.
	 *
	 * @param ctx     Provider context passed at core init.
	 * @param nbytes  Byte count, at most @ref MCUHOME_ENTROPY_IPC_MAX_BYTES.
	 *
	 * @retval 0 request accepted (a response may or may not follow).
	 * @retval -errno the request could not be issued.
	 */
	int (*request)(void *ctx, uint16_t nbytes);
};

/** @brief Policy and wiring handed to mcuhome_entropy_core_init(). */
struct mcuhome_entropy_core_cfg {
	/** Seed provider. */
	const struct mcuhome_entropy_seed_source *src;
	/** Opaque context for the provider's callbacks. */
	void *src_ctx;
	/** Seconds between reseeds; 0 disables periodic reseeding. */
	uint32_t reseed_interval_s;
	/** How long a caller waits for the very first seed, in seconds. */
	uint32_t seed_timeout_s;
};

/** @brief Driver state. Zero-initialized, then mcuhome_entropy_core_init(). */
struct mcuhome_entropy_core {
	struct mcuhome_entropy_core_cfg cfg;

	/* Guards the DRBG and the staging buffer. A spinlock rather than a
	 * mutex because entropy_get_entropy_isr() must be able to take it;
	 * every section under it is a bounded, allocation-free block-cipher
	 * operation.
	 */
	struct k_spinlock lock;

	/* Serializes the blocking first-seed path so mbedtls_ctr_drbg_seed()
	 * is called exactly once even if several threads race for it.
	 * Thread context only.
	 */
	struct k_mutex install_lock;

	/* Given once per staged response, to wake a caller waiting for the
	 * first seed.
	 */
	struct k_sem arrival;

	mbedtls_ctr_drbg_context drbg;

	/* Landing area for the provider, written under @c lock. */
	uint8_t staged[MCUHOME_ENTROPY_SEED_BYTES];
	uint16_t staged_len;

	/* What the DRBG's entropy callback reads. Separate from @c staged so
	 * a late or duplicate response cannot mutate the buffer a seed or
	 * reseed is halfway through consuming.
	 */
	uint8_t feed[MCUHOME_ENTROPY_SEED_BYTES];
	uint16_t feed_len;
	uint16_t feed_off;

	bool seeded;
	bool request_in_flight;
	/* Uptime past which an outstanding request is assumed lost. */
	int64_t request_expiry;
	int64_t next_reseed_uptime;
};

/**
 * @brief Set up state only — touches no transport, issues no request.
 *
 * Safe to call from any init level, including before the transport
 * exists.
 */
void mcuhome_entropy_core_init(struct mcuhome_entropy_core *core,
			       const struct mcuhome_entropy_core_cfg *cfg);

/**
 * @brief Issue the first seed request. Non-blocking.
 *
 * Called once the transport is up. Failure is not fatal — the first
 * mcuhome_entropy_core_get() retries — but it is worth reporting.
 *
 * @retval 0 request issued.
 * @retval -errno from the provider.
 */
int mcuhome_entropy_core_start(struct mcuhome_entropy_core *core);

/**
 * @brief Hand freshly received entropy to the core.
 *
 * Called by the provider from whatever context its response arrives in,
 * including a workqueue or an ISR: it only copies and signals.
 *
 * @param buf  Received bytes.
 * @param len  Number of bytes; a short or oversized delivery is rejected.
 *
 * @retval 0 accepted.
 * @retval -EINVAL wrong length.
 */
int mcuhome_entropy_core_stage(struct mcuhome_entropy_core *core, const uint8_t *buf, size_t len);

/**
 * @brief Tell the core an outstanding request will never be answered.
 *
 * Lets the retry logic re-issue immediately instead of waiting out the
 * poll interval.
 */
void mcuhome_entropy_core_request_failed(struct mcuhome_entropy_core *core);

/**
 * @brief Thread-context entropy, blocking until seeded.
 *
 * Implements entropy_get_entropy() semantics: 0 on success, negative
 * errno otherwise. Never falls back to a weaker source.
 */
int mcuhome_entropy_core_get(struct mcuhome_entropy_core *core, uint8_t *buffer, uint16_t length);

/**
 * @brief ISR-context entropy.
 *
 * Implements entropy_get_entropy_isr() semantics: the number of bytes
 * written, or a negative errno. Serves only from an already-seeded DRBG
 * — waiting for the provider is impossible here, and there is no
 * non-cryptographic fallback by design.
 */
int mcuhome_entropy_core_get_isr(struct mcuhome_entropy_core *core, uint8_t *buffer,
				 uint16_t length, uint32_t flags);

/** @brief Whether the DRBG has been seeded (diagnostics and tests). */
bool mcuhome_entropy_core_is_seeded(struct mcuhome_entropy_core *core);

#ifdef __cplusplus
}
#endif

#endif /* MCUHOME_DRIVERS_ENTROPY_IPC_CORE_H_ */
