/*
 * SPDX-FileCopyrightText: 2026 The MCUHome Contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * Project-level CHIP configuration for the MCUHome prototype.
 */

#pragma once

/* Slots for runtime-registered endpoints (default is 0 — registration
 * fails with CHIP_ERROR_NO_MEMORY without this). */
#define CHIP_DEVICE_CONFIG_DYNAMIC_ENDPOINT_COUNT 4

/* PASE timing: software SPAKE2+ on the plain M33 takes ~3.2 s per step
 * (measured); with the Thread default MRP intervals (SAI 2 s) the
 * commissioner's retransmission budget (~3.3 s) expires first and every
 * commissioning attempt dies as "address unreachable". Advertise larger
 * retry intervals so controllers wait for us; the p256-m driver reduces
 * the compute time as well, this is the safety margin on top. */
#define CHIP_CONFIG_MRP_LOCAL_ACTIVE_RETRY_INTERVAL (4000_ms32)
#define CHIP_CONFIG_MRP_LOCAL_IDLE_RETRY_INTERVAL (5000_ms32)
