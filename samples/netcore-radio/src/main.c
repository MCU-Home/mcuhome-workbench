/*
 * Copyright (c) 2020 Nordic Semiconductor ASA
 *
 * SPDX-FileCopyrightText: 2026 The MCUHome Contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * Network-core radio image, application entry point.
 *
 * The 802.15.4 serialization server needs no start-up code of its own —
 * it is brought up by a SYS_INIT inside hal_nordic
 * (nrf_802154_init_net.c, POST_KERNEL / CONFIG_NRF_802154_SER_RADIO_INIT_PRIO).
 * All this translation unit contributes are the two fault callouts the
 * serialization layer expects the application to define; they are taken
 * unchanged from the upstream sample this image replaces
 * (zephyr/samples/boards/nordic/ieee802154/802154_rpmsg/src/main.c,
 * Apache-2.0), hence the Nordic copyright line above.
 *
 * The MCUHome entropy service is in src/entropy_service.c and starts
 * itself the same way, one init level later.
 */

#include <zephyr/kernel.h>
#include <zephyr/sys/__assert.h>

#include <nrf_802154_serialization_error.h>

void nrf_802154_serialization_error(const nrf_802154_ser_err_data_t *err)
{
	(void)err;
	__ASSERT(false, "802.15.4 serialization error");
	k_oops();
}

void nrf_802154_sl_fault_handler(uint32_t id, int32_t line, const char *err)
{
	__ASSERT(false, "module_id: %u, line: %d, %s", id, line, err);
	k_oops();
}
