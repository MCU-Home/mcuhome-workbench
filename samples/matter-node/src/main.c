/*
 * SPDX-FileCopyrightText: 2026 The MCUHome Contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * MCUHome Matter sample node: application glue in the shape the builder
 * will generate (ADR 0014).
 *
 * Note what is NOT here: no CHIP header, no ember macro, no external
 * attribute callback, no stack lock, and — since the channel layer landed
 * — no value-producing loop either. The device's Matter model is data
 * (src/mcuhome_config.c), its sensor wiring is data (the two tables
 * below), the runtime is the framework (components/matter,
 * components/sensor). This file is plain C on purpose: generated
 * application glue never needs a C++ toolchain.
 *
 * Everything after mcuhome_matter_start() is event-driven — the sensor
 * poller owns its own workqueue, the heartbeat is a kernel timer — so
 * main() has nothing left to do and returns.
 */

#include <zephyr/devicetree.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/drivers/sensor.h>
#include <zephyr/kernel.h>
#include <zephyr/sys/util.h>

#include <mcuhome/channel.h>
#include <mcuhome/matter.h>
#include <mcuhome/matter_tables.h>

/* The generated device configuration (src/mcuhome_config.c). */
extern const struct mcuhome_matter_node mcuhome_node_config;

/* RAM cells behind the two MeasuredValue attributes. A store cell belongs
 * to whoever produces the value — here the sample, in a real device the
 * component/channel instance — never to the generated tables, which only
 * point at them. */
struct mcuhome_attr_store sample_temp_store;
struct mcuhome_attr_store sample_press_store;

/* Both endpoints sample the same chip, so one period covers both. 10 s is
 * a deliberate compromise for a mains-powered DK: fast enough to watch the
 * value move while breathing on the sensor, slow enough that the report
 * deltas below — not the period — decide what reaches the controller. */
#define SAMPLE_PERIOD_MS 10000

/* Matter unit for TemperatureMeasurement::MeasuredValue is 0.01 °C, so a
 * delta of 10 is 0.10 °C: below a BMP180's ±1 °C accuracy, above the noise
 * of a still room. */
#define SAMPLE_TEMP_DELTA 10

/* Matter unit for PressureMeasurement::MeasuredValue is 0.1 kPa == 1 hPa
 * (see src/mcuhome_config.c), so a delta of 1 is 1 hPa — roughly an hour
 * of weather, or 8 m of altitude. */
#define SAMPLE_PRESS_DELTA 1

static const struct mcuhome_channel sample_temp_channel = {
	.store = &sample_temp_store,
	.type = MCUHOME_ATTR_TYPE_INT16S,
	.endpoint_id = 1,
	.cluster_id = 0x0402, /* TemperatureMeasurement */
	.attr_id = 0x0000,    /* MeasuredValue */
	.sample_period_ms = SAMPLE_PERIOD_MS,
	.report_delta = SAMPLE_TEMP_DELTA,
};

static const struct mcuhome_channel sample_press_channel = {
	.store = &sample_press_store,
	.type = MCUHOME_ATTR_TYPE_INT16S,
	.endpoint_id = 2,
	.cluster_id = 0x0403, /* PressureMeasurement */
	.attr_id = 0x0000,    /* MeasuredValue */
	.sample_period_ms = SAMPLE_PERIOD_MS,
	.report_delta = SAMPLE_PRESS_DELTA,
};

/* The sensor peripheral is board-specific: the nRF7002-DK overlay puts a
 * BMP180 on the Arduino I2C header, the dongle build has no I2C bus broken
 * out at all. Generated firmware targets exactly one board and will never
 * carry this guard — it is the price of a sample that builds for two. */
#define SAMPLE_BMP180 DT_NODELABEL(bmp180)

#if DT_NODE_HAS_STATUS_OKAY(SAMPLE_BMP180)

/* The BMP180 exposes its two quantities through one Zephyr device, and the
 * bindings say so by sharing `dev`: the poller then runs one conversion
 * per cycle for both, which also guarantees the pressure reading uses the
 * temperature reading it was compensated with.
 *
 * fetch_channel is SENSOR_CHAN_ALL because the driver asserts on anything
 * else (bmp180_sample_fetch(), drivers/sensor/bosch/bmp180/bmp180.c).
 *
 * The chip reports its die temperature, not a separate ambient sensor —
 * hence SENSOR_CHAN_DIE_TEMP. In a BMP180 the die IS the reference
 * thermometer for the pressure compensation and the package has no
 * self-heating load worth speaking of, so it tracks ambient; the datasheet
 * specifies it as a ±2 °C part. Good enough to prove the path, not good
 * enough to sell as a room thermometer.
 */
static const struct mcuhome_sensor_binding sample_bindings[] = {
	{
		.dev = DEVICE_DT_GET(SAMPLE_BMP180),
		.fetch_channel = SENSOR_CHAN_ALL,
		.zephyr_channel = SENSOR_CHAN_DIE_TEMP,
		/* Zephyr reports °C; Matter wants 0.01 °C. */
		.scale_num = 100,
		.scale_den = 1,
		.offset = 0,
		.channel = &sample_temp_channel,
	},
	{
		.dev = DEVICE_DT_GET(SAMPLE_BMP180),
		.fetch_channel = SENSOR_CHAN_ALL,
		.zephyr_channel = SENSOR_CHAN_PRESS,
		/* Zephyr reports kPa; Matter wants 0.1 kPa. */
		.scale_num = 10,
		.scale_den = 1,
		.offset = 0,
		.channel = &sample_press_channel,
	},
};

#endif /* DT_NODE_HAS_STATUS_OKAY(SAMPLE_BMP180) */

/* LED status — the dongle variant of this sample has no console at all,
 * and even on the DK the RTT log is not always attached: green solid =
 * initializing, green slow blink = fully up, red pulse = stage boundary,
 * red fast blink = a stage failed. */
static const struct gpio_dt_spec led_green = GPIO_DT_SPEC_GET(DT_ALIAS(led0), gpios);
static const struct gpio_dt_spec led_red = GPIO_DT_SPEC_GET(DT_ALIAS(led1), gpios);

/* Heartbeat. A kernel timer rather than a loop in main(): toggling a GPIO
 * is a single register write and safe from the timer ISR, and it keeps the
 * "nothing polls anything from a thread" property this file gained with
 * the channel layer. */
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

	printk("mcuhome: boot, image c1-channels\n");

	err = mcuhome_matter_start(&mcuhome_node_config);
	if (err != 0) {
		printk("mcuhome: Matter bring-up failed: %d\n", err);
		return err;
	}

	/* After Matter, never before: the first sample may publish
	 * immediately and the reporting path has to exist by then. */
#if DT_NODE_HAS_STATUS_OKAY(SAMPLE_BMP180)
	err = mcuhome_sensor_start(sample_bindings, ARRAY_SIZE(sample_bindings));
	if (err != 0) {
		/* A malformed binding table, not a missing sensor — an absent
		 * sensor is handled inside the poller and reports null. */
		printk("mcuhome: sensor channels failed to start: %d\n", err);
		return err;
	}
#else
	printk("mcuhome: no sensor peripheral on this board - endpoints report null\n");
#endif

	k_timer_start(&heartbeat, K_SECONDS(1), K_SECONDS(1)); /* slow blink: fully up */

	printk("mcuhome: up - endpoints 1 (temperature) and 2 (pressure) live\n");

	return 0;
}
