/*
 * SPDX-FileCopyrightText: 2026 The MCUHome Contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * Zephyr entropy driver for cores whose entropy source lives on a peer
 * core, reached over an ipc_service endpoint.
 *
 * The nRF5340 application core is the case this was written for: the RNG
 * peripheral is on the network core only (the application core's
 * devicetree points `zephyr,entropy` at the Bluetooth HCI entropy driver
 * instead, which is no help in a Thread-only build with Bluetooth off).
 * The peer runs the service from samples/netcore-radio; the wire
 * protocol is include/mcuhome/entropy_ipc.h.
 *
 * What this driver hands out is CTR-DRBG output, not raw peer entropy:
 * one exchange per boot and one per reseed interval, rather than one per
 * caller. That keeps psa_generate_random() off the IPC channel entirely
 * and keeps the radio core's RNG out of the critical path of every
 * Matter operation. entropy_ipc_core.c owns that part; this file is the
 * transport and the Zephyr plumbing.
 *
 * Boot is two-phase, and has to be:
 *
 *   POST_KERNEL, CONFIG_ENTROPY_INIT_PRIORITY
 *                 device init — state only, no IPC traffic.
 *   POST_KERNEL, CONFIG_MCUHOME_ENTROPY_IPC_CONNECT_PRIORITY
 *                 SYS_INIT — open the instance, register the endpoint,
 *                 fire the first seed request, return immediately.
 *
 * Nothing in either phase blocks on the peer. That matters because
 * mbedTLS auto-initializes PSA at POST_KERNEL 40 on a single init
 * thread: a driver that waited for IPC from inside that call would
 * deadlock the boot. (Verified that it does not draw randomness there —
 * mbedtls_psa_random_seed() is a no-op under
 * MBEDTLS_PSA_CRYPTO_EXTERNAL_RNG, "the external RNG seeds itself" — but
 * the driver must not depend on that staying true.)
 *
 * Phase one sits at POST_KERNEL rather than the PRE_KERNEL_1 that a
 * state-only init would otherwise allow: the `ipc` phandle makes the
 * ipc_service instance a devicetree dependency of this node, and
 * scripts/build/check_init_priorities.py rejects a device that
 * initializes ahead of one (the backend device comes up at POST_KERNEL /
 * CONFIG_IPC_SERVICE_REG_BACKEND_PRIORITY, 46). The dependency is real —
 * this driver does hold that device — it is only *used* later, and
 * Zephyr has no way for an out-of-tree binding to declare that
 * (_IGNORE_COMPATIBLES in that script is a hardcoded upstream list).
 * Respecting the ordering costs nothing: between POST_KERNEL 40 and this
 * priority nothing in the image asks for randomness, and anything that
 * did would see device_is_ready() == false and fail loud — the same
 * outcome an unseeded PRE_KERNEL_1 device would have produced.
 */

#define DT_DRV_COMPAT mcuhome_entropy_ipc

#include "entropy_ipc_core.h"

#include <zephyr/device.h>
#include <zephyr/devicetree.h>
#include <zephyr/drivers/entropy.h>
#include <zephyr/init.h>
#include <zephyr/ipc/ipc_service.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>

#include <mbedtls/platform_util.h>

#include <mcuhome/entropy_ipc.h>

#include <errno.h>
#include <string.h>

LOG_MODULE_DECLARE(mcuhome_entropy, MCUHOME_ENTROPY_LOG_LEVEL);

BUILD_ASSERT(DT_NUM_INST_STATUS_OKAY(DT_DRV_COMPAT) <= 1,
	     "at most one mcuhome,entropy-ipc node is supported");

struct entropy_ipc_config {
	const struct device *ipc;
};

struct entropy_ipc_data {
	struct mcuhome_entropy_core core;
	struct ipc_ept ept;
	const struct device *dev;
	bool bound;
};

/* ---------------------------------------------------------------------
 * Transport: the seed source the core layer talks to.
 * ------------------------------------------------------------------ */

static void ept_bound(void *priv)
{
	struct entropy_ipc_data *data = priv;

	data->bound = true;
	LOG_DBG("entropy endpoint bound");
}

static void ept_unbound(void *priv)
{
	struct entropy_ipc_data *data = priv;

	/* The peer went away (reset, deregistered). Reseeds will fail until
	 * it comes back and RPMsg rebinds; the DRBG keeps serving from its
	 * existing state in the meantime.
	 */
	data->bound = false;
	mcuhome_entropy_core_request_failed(&data->core);
	LOG_WRN("entropy endpoint unbound by the peer");
}

static void ept_received(const void *raw, size_t len, void *priv)
{
	struct entropy_ipc_data *data = priv;
	struct mcuhome_entropy_ipc_rsp rsp;

	/* The peer sends a zero-length message as part of RPMsg's bind
	 * handshake; the backend consumes those itself, so anything
	 * arriving here is meant to be a response.
	 */
	if (len < MCUHOME_ENTROPY_IPC_RSP_ERR_LEN || len > sizeof(rsp)) {
		LOG_ERR("malformed entropy response (%zu bytes)", len);
		mcuhome_entropy_core_request_failed(&data->core);
		return;
	}

	/* Copy out of the transport buffer before doing anything with it:
	 * it is only valid for the duration of this callback, and its
	 * alignment is the backend's business, not ours.
	 */
	memcpy(&rsp, raw, len);

	if (rsp.magic != MCUHOME_ENTROPY_IPC_MAGIC_RSP) {
		/* Version skew between the two images, or a foreign sender.
		 * Loud, because the images are flashed as a pair and a
		 * mismatch means one of them is stale.
		 */
		LOG_ERR("entropy protocol mismatch: peer magic 0x%04x, expected 0x%04x "
			"(is the network core image the matching build?)",
			rsp.magic, MCUHOME_ENTROPY_IPC_MAGIC_RSP);
		mcuhome_entropy_core_request_failed(&data->core);
		return;
	}

	if (rsp.nbytes == 0U || len != MCUHOME_ENTROPY_IPC_RSP_LEN(rsp.nbytes)) {
		LOG_ERR("peer refused the entropy request (nbytes %u, message %zu bytes)",
			rsp.nbytes, len);
		mcuhome_entropy_core_request_failed(&data->core);
		return;
	}

	(void)mcuhome_entropy_core_stage(&data->core, rsp.bytes, rsp.nbytes);

	/* Do not leave seed material on this stack frame. */
	mbedtls_platform_zeroize(&rsp, sizeof(rsp));
}

static int transport_request(void *ctx, uint16_t nbytes)
{
	struct entropy_ipc_data *data = ctx;
	const struct mcuhome_entropy_ipc_req req = {
		.magic = MCUHOME_ENTROPY_IPC_MAGIC_REQ,
		.nbytes = nbytes,
	};
	int err;

	if (!data->bound) {
		return -ENOTCONN;
	}

	err = ipc_service_send(&data->ept, &req, sizeof(req));
	if (err < 0) {
		LOG_WRN("entropy request send failed (%d)", err);
		return err;
	}

	return 0;
}

static const struct mcuhome_entropy_seed_source ipc_seed_source = {
	.name = "netcore entropy service",
	.request = transport_request,
};

/* ---------------------------------------------------------------------
 * Zephyr entropy driver API.
 * ------------------------------------------------------------------ */

static int entropy_ipc_get(const struct device *dev, uint8_t *buffer, uint16_t length)
{
	struct entropy_ipc_data *data = dev->data;

	return mcuhome_entropy_core_get(&data->core, buffer, length);
}

static int entropy_ipc_get_isr(const struct device *dev, uint8_t *buffer, uint16_t length,
			       uint32_t flags)
{
	struct entropy_ipc_data *data = dev->data;

	return mcuhome_entropy_core_get_isr(&data->core, buffer, length, flags);
}

static DEVICE_API(entropy, entropy_ipc_api) = {
	.get_entropy = entropy_ipc_get,
	.get_entropy_isr = entropy_ipc_get_isr,
};

/* Phase 1: state only, no IPC traffic. */
static int entropy_ipc_init(const struct device *dev)
{
	struct entropy_ipc_data *data = dev->data;
	const struct mcuhome_entropy_core_cfg cfg = {
		.src = &ipc_seed_source,
		.src_ctx = data,
		.reseed_interval_s = CONFIG_MCUHOME_ENTROPY_RESEED_INTERVAL_S,
		.seed_timeout_s = CONFIG_MCUHOME_ENTROPY_SEED_TIMEOUT_S,
	};

	data->dev = dev;
	mcuhome_entropy_core_init(&data->core, &cfg);

	return 0;
}

static struct entropy_ipc_data entropy_ipc_data_0;

static const struct entropy_ipc_config entropy_ipc_config_0 = {
	.ipc = DEVICE_DT_GET(DT_INST_PHANDLE(0, ipc)),
};

BUILD_ASSERT(CONFIG_ENTROPY_INIT_PRIORITY > CONFIG_IPC_SERVICE_REG_BACKEND_PRIORITY,
	     "the entropy device must initialize after the ipc_service backend it depends on");

DEVICE_DT_INST_DEFINE(0, entropy_ipc_init, NULL, &entropy_ipc_data_0, &entropy_ipc_config_0,
		      POST_KERNEL, CONFIG_ENTROPY_INIT_PRIORITY, &entropy_ipc_api);

/* Phase 2: connect. */
static struct ipc_ept_cfg entropy_ipc_ept_cfg = {
	.name = MCUHOME_ENTROPY_IPC_EPT_NAME,
	.cb =
		{
			.bound = ept_bound,
			.unbound = ept_unbound,
			.received = ept_received,
		},
	.priv = &entropy_ipc_data_0,
};

static int entropy_ipc_connect(void)
{
	const struct device *const dev = DEVICE_DT_INST_GET(0);
	const struct entropy_ipc_config *cfg = dev->config;
	struct entropy_ipc_data *data = dev->data;
	int err;

	if (!device_is_ready(cfg->ipc)) {
		LOG_ERR("IPC instance %s not ready", cfg->ipc->name);
		return -ENODEV;
	}

	/* Idempotent: on nRF53 the 802.15.4 spinel host has usually opened
	 * this instance already (POST_KERNEL / CONFIG_IEEE802154_NRF5_INIT_PRIO),
	 * and on a build without 802.15.4 we are the ones who open it. The
	 * host role does not wait for the peer, so neither ordering blocks
	 * the init thread.
	 */
	err = ipc_service_open_instance(cfg->ipc);
	if (err < 0 && err != -EALREADY) {
		LOG_ERR("failed to open IPC instance (%d)", err);
		return err;
	}

	err = ipc_service_register_endpoint(cfg->ipc, &data->ept, &entropy_ipc_ept_cfg);
	if (err < 0) {
		LOG_ERR("failed to register the '%s' endpoint (%d)", MCUHOME_ENTROPY_IPC_EPT_NAME,
			err);
		return err;
	}

	/* Asynchronous by design: if the peer has already announced the
	 * endpoint, registering above bound it synchronously and this
	 * request goes out now; if not, it fails with -ENOTCONN and the
	 * first entropy_get_entropy() retries until its timeout.
	 */
	(void)mcuhome_entropy_core_start(&data->core);

	return 0;
}

BUILD_ASSERT(CONFIG_MCUHOME_ENTROPY_IPC_CONNECT_PRIORITY > CONFIG_IPC_SERVICE_REG_BACKEND_PRIORITY,
	     "the entropy endpoint must be registered after the ipc_service backend exists");

SYS_INIT(entropy_ipc_connect, POST_KERNEL, CONFIG_MCUHOME_ENTROPY_IPC_CONNECT_PRIORITY);
