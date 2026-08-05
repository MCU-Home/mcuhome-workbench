/*
 * SPDX-FileCopyrightText: 2026 The MCUHome Contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * INSECURE prototype entropy driver (xoshiro128**-based PRNG).
 *
 * The nRF5340 application core has no TRNG peripheral (the RNG lives on
 * the network core; production designs use the CryptoCell via PSA or
 * entropy forwarding). Upstream Zephyr offers neither, so this driver
 * exists purely to let the Matter boot experiment proceed. It is NOT
 * cryptographically secure and must never ship in real firmware.
 */

#define DT_DRV_COMPAT mcuhome_proto_entropy

#include <zephyr/device.h>
#include <zephyr/drivers/entropy.h>
#include <zephyr/kernel.h>
#include <zephyr/sys/printk.h>

static uint32_t s[4];

static inline uint32_t rotl(const uint32_t x, int k)
{
	return (x << k) | (x >> (32 - k));
}

static uint32_t next(void)
{
	const uint32_t result = rotl(s[1] * 5, 7) * 9;
	const uint32_t t = s[1] << 9;

	s[2] ^= s[0];
	s[3] ^= s[1];
	s[1] ^= s[2];
	s[0] ^= s[3];
	s[2] ^= t;
	s[3] = rotl(s[3], 11);
	return result;
}

static int proto_entropy_get(const struct device *dev, uint8_t *buffer, uint16_t length)
{
	ARG_UNUSED(dev);
	while (length > 0) {
		uint32_t r = next();
		size_t n = MIN(length, sizeof(r));

		memcpy(buffer, &r, n);
		buffer += n;
		length -= n;
	}
	return 0;
}

static int proto_entropy_init(const struct device *dev)
{
	ARG_UNUSED(dev);
	/* Weak seed: cycle counter, uptime and stack address. */
	uint32_t marker = 0;

	s[0] = k_cycle_get_32() ^ 0xdeadbeef;
	s[1] = (uint32_t)(uintptr_t)&marker;
	s[2] = k_cycle_get_32() ^ 0x5f3759df;
	s[3] = k_uptime_get_32() ^ 0xa5a5a5a5;
	printk("proto-entropy: WARNING insecure PRNG entropy source active\n");
	return 0;
}

static DEVICE_API(entropy, proto_entropy_api) = {
	.get_entropy = proto_entropy_get,
};

DEVICE_DT_INST_DEFINE(0, proto_entropy_init, NULL, NULL, NULL, PRE_KERNEL_1,
		      CONFIG_KERNEL_INIT_PRIORITY_DEVICE, &proto_entropy_api);
