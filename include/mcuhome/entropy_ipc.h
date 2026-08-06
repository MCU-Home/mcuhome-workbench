/*
 * SPDX-FileCopyrightText: 2026 The MCUHome Contributors
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * @file
 * @brief Wire protocol of the MCUHome netcore entropy service.
 *
 * A multi-core SoC whose application core has no entropy peripheral (the
 * nRF5340 is the case that motivated this) can still reach a real TRNG:
 * the peripheral sits on the other core. This header defines the tiny
 * request/response framing both cores agree on. It is plain C with no
 * Zephyr and no MCUHome dependencies, precisely so the radio-core image
 * — which shares nothing else with the application — can include it.
 *
 * Transport: one `ipc_service` endpoint named
 * @ref MCUHOME_ENTROPY_IPC_EPT_NAME on the instance that already carries
 * the 802.15.4 spinel channel (`ipc0` on nRF5340). It is a second
 * endpoint on an existing instance, not a second instance: no extra
 * shared memory, no extra mailbox channel.
 *
 * Exchange, strictly request/response, one outstanding request at a time:
 *
 *     app core                                  radio core
 *     --------------------------------------------------------------
 *     struct mcuhome_entropy_ipc_req  --------->
 *                                               entropy_get_entropy()
 *                                     <-------- struct mcuhome_entropy_ipc_rsp
 *
 * @note **Byte order.** Both peers are cores of the same SoC and share
 *       endianness, so the u16 fields travel in native order and no
 *       conversion is performed. A future asymmetric peer would have to
 *       bump @ref MCUHOME_ENTROPY_IPC_VERSION and define an order.
 *
 * @note **What this protocol is not.** The bytes on the wire are raw
 *       entropy, used only to seed and reseed a DRBG on the requesting
 *       core — they are never handed to a caller directly. The shared
 *       memory the transport uses is visible to both cores by
 *       construction; the threat model that matters here is a wedged or
 *       absent peer (handled: the requester fails loud), not an
 *       eavesdropper on the SoC's internal RAM.
 *
 * @note **Provider seam.** The IPC service defined here is the
 *       always-available fallback provider: it needs nothing but the
 *       cores' own silicon and an already-present IPC instance. A build
 *       may later obtain its seed from a different source — on Nordic
 *       silicon the `nrf_cc3xx` CryptoCell blob is the candidate (ADR
 *       0013 governs whether a blob may be used at all). The consumer
 *       side is written against a seed-source interface for that reason:
 *       see `struct mcuhome_entropy_seed_source` in
 *       `drivers/entropy/entropy_mcuhome_ipc.c`. Swapping the provider
 *       changes where the 48 seed bytes come from and nothing else — the
 *       DRBG, the reseed policy and the Zephyr entropy API surface stay
 *       as they are.
 */

#ifndef MCUHOME_ENTROPY_IPC_H_
#define MCUHOME_ENTROPY_IPC_H_

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief Protocol version.
 *
 * Carried in the low byte of every magic value, so a version skew
 * between the two images is caught on the very first message instead of
 * being interpreted as garbage entropy. Bump on any layout change.
 */
#define MCUHOME_ENTROPY_IPC_VERSION 1U

/** @brief Magic of a request (app core -> radio core). */
#define MCUHOME_ENTROPY_IPC_MAGIC_REQ ((uint16_t)(0xE500U | MCUHOME_ENTROPY_IPC_VERSION))

/** @brief Magic of a response (radio core -> app core). */
#define MCUHOME_ENTROPY_IPC_MAGIC_RSP ((uint16_t)(0x5E00U | MCUHOME_ENTROPY_IPC_VERSION))

/**
 * @brief Name of the `ipc_service` endpoint carrying this protocol.
 *
 * Must fit RPMsg's 32-byte name field (`RPMSG_NAME_SIZE`) and must be
 * identical on both cores — RPMsg binds endpoints by name.
 */
#define MCUHOME_ENTROPY_IPC_EPT_NAME "mcuhome_entropy"

/**
 * @brief Largest number of bytes one response may carry.
 *
 * Bounds the static buffers on both cores. Sized to cover a CTR-DRBG
 * instantiation (32 B entropy + 16 B nonce) in a single exchange with
 * room to spare, while staying far below the RPMsg buffer size.
 */
#define MCUHOME_ENTROPY_IPC_MAX_BYTES 64U

/** @brief Request: give me @c nbytes bytes of entropy. */
struct mcuhome_entropy_ipc_req {
	/** @ref MCUHOME_ENTROPY_IPC_MAGIC_REQ. */
	uint16_t magic;
	/** Requested byte count, 1..@ref MCUHOME_ENTROPY_IPC_MAX_BYTES. */
	uint16_t nbytes;
};

/**
 * @brief Response: here are @c nbytes bytes of entropy.
 *
 * Only the first @c nbytes bytes of @c bytes are transmitted — use
 * @ref MCUHOME_ENTROPY_IPC_RSP_LEN to size the send.
 *
 * @c nbytes == 0 is the error response: the provider could not serve the
 * request (unknown magic, out-of-range length, local entropy driver
 * failure). There is deliberately no error code — the requester's only
 * sane reaction to any of them is to fail loud, and a code would invite
 * a caller to special-case its way into using unseeded output.
 */
struct mcuhome_entropy_ipc_rsp {
	/** @ref MCUHOME_ENTROPY_IPC_MAGIC_RSP. */
	uint16_t magic;
	/** Number of valid bytes in @c bytes; 0 signals failure. */
	uint16_t nbytes;
	/** The entropy itself. */
	uint8_t bytes[MCUHOME_ENTROPY_IPC_MAX_BYTES];
};

/** @brief Wire length of a response carrying @p n payload bytes. */
#define MCUHOME_ENTROPY_IPC_RSP_LEN(n) (offsetof(struct mcuhome_entropy_ipc_rsp, bytes) + (n))

/** @brief Wire length of a response carrying no payload (error case). */
#define MCUHOME_ENTROPY_IPC_RSP_ERR_LEN MCUHOME_ENTROPY_IPC_RSP_LEN(0)

#ifdef __cplusplus
}
#endif

#endif /* MCUHOME_ENTROPY_IPC_H_ */
