/*
 * SPDX-FileCopyrightText: 2026 The MCUHome Contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * MCUHome Matter sample node: application glue in the shape the builder
 * will generate (ADR 0014).
 *
 * Note what is NOT here: no CHIP header, no ember macro, no external
 * attribute callback, no stack lock. The device's Matter model is data
 * (src/mcuhome_config.c), the runtime is the framework
 * (components/matter). This file is plain C on purpose — generated
 * application glue never needs a C++ toolchain.
 *
 * The simulated sensor stands in for a real channel: write the value into
 * the attribute's store cell, then report the change.
 */

#include <zephyr/drivers/gpio.h>
#include <zephyr/kernel.h>

#include <mcuhome/matter.h>
#include <mcuhome/matter_tables.h>

/* The generated device configuration (src/mcuhome_config.c). */
extern const struct mcuhome_matter_node mcuhome_node_config;

/* RAM cell behind EP1 / TemperatureMeasurement / MeasuredValue. The store
 * cell belongs to whoever produces the value — here the sample, in a real
 * device the component/channel instance — never to the generated tables,
 * which only point at it. */
struct mcuhome_attr_store sample_temp_store;

#define SAMPLE_ENDPOINT_ID 1
#define SAMPLE_TEMP_CLUSTER_ID 0x0402 /* TemperatureMeasurement */
#define SAMPLE_TEMP_ATTR_ID 0x0000    /* MeasuredValue */

/* Matter unit for temperature is 0.01 °C. */
#define SAMPLE_TEMP_START 2150
#define SAMPLE_TEMP_STEP 25
#define SAMPLE_TEMP_MAX 2600
#define SAMPLE_TEMP_WRAP 2000
#define SAMPLE_TEMP_PERIOD_S 30

/* LED status — the dongle variant of this sample has no console at all,
 * and even on the DK the RTT log is not always attached: green solid =
 * initializing, green slow blink = fully up, red pulse = stage boundary,
 * red fast blink = a stage failed. */
static const struct gpio_dt_spec led_green = GPIO_DT_SPEC_GET(DT_ALIAS(led0), gpios);
static const struct gpio_dt_spec led_red = GPIO_DT_SPEC_GET(DT_ALIAS(led1), gpios);

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
	int16_t temp_centi_c = SAMPLE_TEMP_START;
	int err;

	gpio_pin_configure_dt(&led_green, GPIO_OUTPUT_INACTIVE);
	gpio_pin_configure_dt(&led_red, GPIO_OUTPUT_INACTIVE);
	gpio_pin_set_dt(&led_green, 1); /* solid green: initializing */

	printk("mcuhome: boot, image a4-tables\n");

	err = mcuhome_matter_start(&mcuhome_node_config);
	if (err != 0) {
		printk("mcuhome: Matter bring-up failed: %d\n", err);
		return err;
	}

	printk("mcuhome: up — endpoint %d live, simulating values\n", SAMPLE_ENDPOINT_ID);

	while (1) {
		for (int i = 0; i < SAMPLE_TEMP_PERIOD_S; i++) {
			gpio_pin_toggle_dt(&led_green); /* slow blink: fully up */
			k_sleep(K_SECONDS(1));
		}

		temp_centi_c += SAMPLE_TEMP_STEP;
		if (temp_centi_c > SAMPLE_TEMP_MAX) {
			temp_centi_c = SAMPLE_TEMP_WRAP;
		}

		mcuhome_attr_store_publish_i16s(&sample_temp_store, temp_centi_c);
		mcuhome_matter_attr_changed(SAMPLE_ENDPOINT_ID, SAMPLE_TEMP_CLUSTER_ID,
					    SAMPLE_TEMP_ATTR_ID);

		printk("mcuhome: alive, simulated temp %d.%02d C\n", temp_centi_c / 100,
		       temp_centi_c % 100);
	}

	return 0;
}
