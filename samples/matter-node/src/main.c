/*
 * SPDX-FileCopyrightText: 2026 The MCUHome Contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * MCUHome Matter sample node: application glue, and nothing else.
 *
 * Note what is NOT here: no CHIP header, no ember macro, no external
 * attribute callback, no stack lock, no value-producing loop — and, since
 * the builder generates this device's configuration, no tables either. The
 * device's Matter model and its sensor wiring are data
 * (src/mcuhome_config.c, generated from
 * docs/design/examples/00-bmp180-two-endpoints.yaml), the runtime is the
 * framework (components/matter, components/sensor). What remains below is
 * the LED status this board needs because it has no attached console, plus
 * three calls.
 *
 * Everything after mcuhome_matter_start() is event-driven — the sensor
 * poller owns its own workqueue, the heartbeat is a kernel timer — so
 * main() has nothing left to do and returns.
 */

#include <zephyr/devicetree.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/kernel.h>

#include <mcuhome/channel.h>
#include <mcuhome/matter.h>

#include "mcuhome_config.h"

/* LED status — the RTT log is not always attached, and a board without a
 * console has nothing else: green solid = initializing, green slow blink =
 * fully up, red pulse = stage boundary, red fast blink = a stage failed. */
static const struct gpio_dt_spec led_green = GPIO_DT_SPEC_GET(DT_ALIAS(led0), gpios);
static const struct gpio_dt_spec led_red = GPIO_DT_SPEC_GET(DT_ALIAS(led1), gpios);

/* Heartbeat. A kernel timer rather than a loop in main(): toggling a GPIO
 * is a single register write and safe from the timer ISR, and it keeps the
 * "nothing polls anything from a thread" property this file has. */
static void heartbeat_expiry(struct k_timer *timer)
{
	ARG_UNUSED(timer);
	gpio_pin_toggle_dt(&led_green);
}

static K_TIMER_DEFINE(heartbeat, heartbeat_expiry, NULL);

/* Override of the framework's weak default hook. Kept from the bring-up
 * era: it is the only diagnostic a board without a console has. */
void mcuhome_matter_stage(const char *name, int err)
{
	if (err == 0) {
		printk("mcuhome: stage %s OK\n", name);
		gpio_pin_set_dt(&led_red, 1);
		k_sleep(K_MSEC(80));
		gpio_pin_set_dt(&led_red, 0);
		return;
	}

	printk("mcuhome: stage %s FAILED (%d)\n", name, err);
	for (int i = 0; i < 10; i++) {
		gpio_pin_toggle_dt(&led_red);
		k_sleep(K_MSEC(100));
	}
	gpio_pin_set_dt(&led_red, 0);
}

int main(void)
{
	int err;

	gpio_pin_configure_dt(&led_green, GPIO_OUTPUT_INACTIVE);
	gpio_pin_configure_dt(&led_red, GPIO_OUTPUT_INACTIVE);
	gpio_pin_set_dt(&led_green, 1); /* solid green: initializing */

	printk("mcuhome: boot, matter-node sample\n");

	err = mcuhome_matter_start(&mcuhome_node_config);
	if (err != 0) {
		printk("mcuhome: Matter bring-up failed: %d\n", err);
		return err;
	}

	/* After Matter, never before: the first sample may publish
	 * immediately and the reporting path has to exist by then. */
	err = mcuhome_sensor_start(mcuhome_sensor_bindings, mcuhome_sensor_binding_count);
	mcuhome_matter_stage("SensorStart", err);
	if (err != 0) {
		/* A malformed binding table or a binding/table ID mismatch
		 * (mcuhome_matter_attr_store_lookup(), <mcuhome/channel.h>) —
		 * not a missing sensor, which the poller handles internally
		 * and reports as null. Visible (LED + log) but not fatal: the
		 * node must stay commissionable even with broken sensor
		 * wiring, so its endpoints simply keep reporting null. */
		printk("mcuhome: sensor channels failed to start: %d\n", err);
	}

	k_timer_start(&heartbeat, K_SECONDS(1), K_SECONDS(1)); /* slow blink: fully up */

	printk("mcuhome: up - endpoints 1 (temperature) and 2 (pressure) live\n");

	return 0;
}
