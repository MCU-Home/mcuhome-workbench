/*
 * SPDX-FileCopyrightText: 2026 The MCUHome Contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * MCUHome entropy service (network-core side).
 *
 * Serves `struct mcuhome_entropy_ipc_req` on a second endpoint of the
 * IPC instance that already carries the 802.15.4 spinel channel, using
 * this core's RNG peripheral (the application core has none — see
 * include/mcuhome/entropy_ipc.h).
 *
 * Everything here is statically allocated and bounded: one endpoint, one
 * request slot, one response buffer, one thread. No heap.
 */

#include <zephyr/device.h>
#include <zephyr/devicetree.h>
#include <zephyr/drivers/entropy.h>
#include <zephyr/init.h>
#include <zephyr/ipc/ipc_service.h>
#include <zephyr/kernel.h>
#include <zephyr/sys/atomic.h>

#include <mcuhome/entropy_ipc.h>

#include <string.h>

/* Attach to the instance the radio serialization uses, so the entropy
 * endpoint rides on the channel that is guaranteed to exist in this
 * image rather than on an assumed node label.
 */
#if DT_HAS_CHOSEN(nordic_802154_spinel_ipc)
#define ENTROPY_IPC_NODE DT_CHOSEN(nordic_802154_spinel_ipc)
#else
#define ENTROPY_IPC_NODE DT_NODELABEL(ipc0)
#endif

/* Init order: after hal_nordic's serialization_init
 * (POST_KERNEL / CONFIG_NRF_802154_SER_RADIO_INIT_PRIO), which is what
 * actually opens the IPC instance. On this core the backend has the
 * `remote` role, and opening spins until the application core has set up
 * its side (rpmsg_virtio_wait_remote_ready). Running after the radio
 * server means our ipc_service_open_instance() only ever confirms an
 * already-open instance (-EALREADY) instead of adding a second place
 * that can block the single init thread.
 */
#define ENTROPY_SERVICE_INIT_PRIO 60

#if defined(CONFIG_NRF_802154_SER_RADIO_INIT_PRIO)
BUILD_ASSERT(ENTROPY_SERVICE_INIT_PRIO > CONFIG_NRF_802154_SER_RADIO_INIT_PRIO,
	     "entropy service must initialize after the 802.15.4 serialization server");
#endif

/* Serving a request means calling entropy_get_entropy(), which blocks
 * until the RNG peripheral has produced the requested bytes (tens of
 * microseconds each). That must not happen on the IPC mailbox
 * workqueue: the spinel channel's receive path shares that thread, so
 * blocking it stalls the radio. The request is therefore handed to this
 * dedicated thread, which also owns every transmission — the same reason
 * the upstream spinel backend keeps its own send thread.
 *
 * Stack: entropy_get_entropy() and ipc_service_send() are both shallow
 * (a memcpy into a shared-memory buffer at the bottom); 1 KB matches
 * what the spinel send thread needs for a comparable path.
 */
#define ENTROPY_SERVICE_STACK_SIZE 1024

/* Preemptible and low: the radio server must always win. */
#define ENTROPY_SERVICE_PRIO 10

static const struct device *const entropy_dev = DEVICE_DT_GET(DT_CHOSEN(zephyr_entropy));

static struct ipc_ept ept;

/* Handoff from the IPC receive callback to the service thread. One slot,
 * so at most one request is in flight; further requests are dropped
 * while it is occupied and the requester's timeout deals with it.
 * `req_nbytes` is written before the semaphore is given and read after
 * it is taken, which orders the two accesses.
 */
static K_SEM_DEFINE(req_sem, 0, 1);
static atomic_t req_pending;
static uint16_t req_nbytes;

/* Thread-owned response buffer. */
static struct mcuhome_entropy_ipc_rsp rsp;

static void ept_received(const void *data, size_t len, void *priv)
{
	struct mcuhome_entropy_ipc_req req;
	uint16_t nbytes = 0U;

	ARG_UNUSED(priv);

	/* Copy out first: the RPMsg buffer's alignment is the transport's
	 * business, and it is released as soon as this callback returns.
	 */
	if (len == sizeof(req)) {
		memcpy(&req, data, sizeof(req));

		if (req.magic == MCUHOME_ENTROPY_IPC_MAGIC_REQ && req.nbytes > 0U &&
		    req.nbytes <= MCUHOME_ENTROPY_IPC_MAX_BYTES) {
			nbytes = req.nbytes;
		}
	}

	/* A malformed request still gets answered (nbytes == 0), so a
	 * version-skewed peer sees a protocol error instead of a timeout.
	 */
	if (!atomic_cas(&req_pending, 0, 1)) {
		return;
	}

	req_nbytes = nbytes;
	k_sem_give(&req_sem);
}

static struct ipc_ept_cfg ept_cfg = {
	.name = MCUHOME_ENTROPY_IPC_EPT_NAME,
	.cb =
		{
			.received = ept_received,
		},
};

static void entropy_service_thread(void *p1, void *p2, void *p3)
{
	ARG_UNUSED(p1);
	ARG_UNUSED(p2);
	ARG_UNUSED(p3);

	while (true) {
		uint16_t nbytes;

		k_sem_take(&req_sem, K_FOREVER);
		nbytes = req_nbytes;

		rsp.magic = MCUHOME_ENTROPY_IPC_MAGIC_RSP;
		rsp.nbytes = 0U;

		if (nbytes > 0U && entropy_get_entropy(entropy_dev, rsp.bytes, nbytes) == 0) {
			rsp.nbytes = nbytes;
		}

		(void)ipc_service_send(&ept, &rsp, MCUHOME_ENTROPY_IPC_RSP_LEN(rsp.nbytes));

		/* Do not leave seed material lying in this core's RAM. */
		memset(rsp.bytes, 0, sizeof(rsp.bytes));

		atomic_clear(&req_pending);
	}
}

K_THREAD_DEFINE(entropy_service_tid, ENTROPY_SERVICE_STACK_SIZE, entropy_service_thread, NULL, NULL,
		NULL, ENTROPY_SERVICE_PRIO, 0, 0);

static int entropy_service_init(void)
{
	const struct device *const ipc = DEVICE_DT_GET(ENTROPY_IPC_NODE);
	int err;

	if (!device_is_ready(ipc) || !device_is_ready(entropy_dev)) {
		return -ENODEV;
	}

	err = ipc_service_open_instance(ipc);
	if (err < 0 && err != -EALREADY) {
		return err;
	}

	/* Registering announces the endpoint by name; the application core
	 * binds to it whenever it comes up, before or after this point.
	 *
	 * A failure here is reported but not fatal: this image's primary
	 * job is the radio, and killing it would take the whole node down
	 * instead of only its crypto. This core has no log transport
	 * (CONFIG_LOG=n, no console), so the loud failure happens on the
	 * application core, which times out waiting for a seed and refuses
	 * to hand out randomness.
	 */
	return ipc_service_register_endpoint(ipc, &ept, &ept_cfg);
}

SYS_INIT(entropy_service_init, POST_KERNEL, ENTROPY_SERVICE_INIT_PRIO);
