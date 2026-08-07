/*
 * SPDX-FileCopyrightText: 2026 The MCUHome Contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * The generic MCUHome application main — the glue every generated device
 * shares.
 *
 * `mcuhome build` does not generate this file. It generates the device
 * *configuration* next to it (`mcuhome_config.c/.h`: the Matter tables and
 * the channel bindings) and a CMakeLists that compiles this main against
 * it, which is the whole point of the thin-codegen principle
 * (docs/design/builder-pipeline.md §1): behavior is written once, reviewed
 * once and debugged once, here; only data varies per device.
 *
 * Consequently nothing below may know anything about a specific device.
 * What it does know is the two contracts every device speaks:
 * <mcuhome/matter.h> plus the generated `mcuhome_node_config`, and — for
 * devices that have sensor channels — <mcuhome/channel.h> plus the
 * generated binding table. Which of the two are present is a Kconfig fact
 * (CONFIG_MCUHOME_MATTER, CONFIG_MCUHOME_SENSOR), and the builder derives
 * both symbols from the same YAML that produced the tables, so the #ifdefs
 * below never disagree with the generated file.
 *
 * Not here, on purpose:
 *
 * - No board-specific status output. The sample overrides the framework's
 *   weak mcuhome_matter_stage() hook to blink LEDs because that board has
 *   no attached console (samples/matter-node/src/main.c); a generated
 *   application cannot assume an led0 alias exists, so it keeps the
 *   framework's default hook, which logs. A device that wants LEDs gets
 *   them from a component, not from this file.
 * - No loop. Everything after bring-up is event-driven: the sensor poller
 *   owns its workqueue, Matter owns the CHIP event loop. main() has
 *   nothing left to do and returns.
 */

#if defined(__has_include)
#if !__has_include("mcuhome_config.h")
#error "No generated device configuration next to this file. mcuhome/app is not a " \
	"standalone Zephyr application: it holds the generic main that the MCUHome " \
	"builder compiles together with the configuration it generates from a YAML " \
	"device description. Run `mcuhome build <device>` and build what it writes."
#endif
#endif

#include <zephyr/kernel.h>
#include <zephyr/sys/printk.h>

#include "mcuhome_config.h"

#ifdef CONFIG_MCUHOME_MATTER
#include <mcuhome/matter.h>
#endif

#ifdef CONFIG_MCUHOME_SENSOR
#include <mcuhome/channel.h>
#endif

int main(void)
{
#ifdef CONFIG_MCUHOME_MATTER
	int err;
#endif

	printk("mcuhome: boot, " MCUHOME_DEVICE_NAME "\n");

#ifdef CONFIG_MCUHOME_MATTER
	err = mcuhome_matter_start(&mcuhome_node_config);
	if (err != 0) {
		printk("mcuhome: Matter bring-up failed: %d\n", err);
		return err;
	}

#ifdef CONFIG_MCUHOME_SENSOR
	/* After Matter, never before: the first sample may publish
	 * immediately and the reporting path has to exist by then.
	 */
	err = mcuhome_sensor_start(mcuhome_sensor_bindings, mcuhome_sensor_binding_count);
	mcuhome_matter_stage("SensorStart", err);
	if (err != 0) {
		/* A malformed binding table or a binding/table ID mismatch
		 * (mcuhome_matter_attr_store_lookup(), <mcuhome/channel.h>) —
		 * not a missing sensor, which the poller handles internally
		 * and reports as null. Visible but not fatal: the node must
		 * stay commissionable even with broken sensor wiring, so its
		 * endpoints simply keep reporting null.
		 */
		printk("mcuhome: sensor channels failed to start: %d\n", err);
	}
#endif /* CONFIG_MCUHOME_SENSOR */

	printk("mcuhome: up, %u endpoint(s)\n", (unsigned int)mcuhome_node_config.endpoint_count);
#else
	printk("mcuhome: up, no network configured\n");
#endif /* CONFIG_MCUHOME_MATTER */

	return 0;
}
