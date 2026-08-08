/*
 * SPDX-FileCopyrightText: 2026 The MCUHome Contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * Image self-confirmation (<mcuhome/health.h>, ADR 0015 health amendment).
 *
 * MCUboot's swap-with-revert scheme installs a downloaded image as a TEST
 * image: it runs once, and unless something calls
 * boot_write_img_confirmed() the next boot swaps the previous image back.
 * That single mechanism is what makes a bad over-the-air update survivable
 * on a device with no cable attached — and it only works if a healthy
 * image confirms itself.
 *
 * "Healthy" here means: the device reached its operational point and
 * stayed there for CONFIG_MCUHOME_IMAGE_CONFIRM_DELAY_S. Both halves
 * matter. Confirming at the operational point alone would confirm an
 * image that reaches it and then immediately faults; waiting without ever
 * reaching it would never confirm a working device either.
 *
 * The window is deliberately narrow, and it is a floor rather than a
 * ceiling: an image that keeps crashing after confirmation is what the
 * fault counters of the same amendment are for, and those are a later
 * work item. What is here is the part MCUboot already implements.
 */

#include <stdbool.h>

#include <zephyr/dfu/mcuboot.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>

#include <mcuhome/health.h>

LOG_MODULE_DECLARE(mcuhome_health, CONFIG_LOG_DEFAULT_LEVEL);

#define CONFIRM_DELAY_MS (CONFIG_MCUHOME_IMAGE_CONFIRM_DELAY_S * 1000U)

/* Sampled once, before anything can confirm the image, so that
 * mcuhome_health_image_pending() keeps answering the question a caller
 * means — "did this boot start as a test image" — rather than the one
 * boot_is_img_confirmed() answers after the fact. */
static bool booted_as_test_image;
static bool confirm_armed;

static void confirm_work_handler(struct k_work *work);
static K_WORK_DELAYABLE_DEFINE(confirm_work, confirm_work_handler);

static void confirm_work_handler(struct k_work *work)
{
	int rc;

	ARG_UNUSED(work);

	/* Re-checked rather than trusted: between arming and firing, MCUboot's
	 * trailer is the authority and something else (serial recovery, a
	 * future maintenance channel) may have confirmed the image already.
	 * Writing the trailer twice is harmless but the log line would lie. */
	if (boot_is_img_confirmed()) {
		LOG_DBG("image already confirmed, nothing to do");
		return;
	}

	rc = boot_write_img_confirmed();
	if (rc != 0) {
		/* The next boot reverts. Loud, because that is a surprise the
		 * user will otherwise experience as "the update did not
		 * stick" with nothing to point at. */
		LOG_ERR("could not confirm this image (%d) — MCUboot will REVERT to the "
			"previous one on the next boot",
			rc);
		return;
	}
	LOG_INF("image confirmed after %u s of healthy uptime — the update is now permanent",
		CONFIG_MCUHOME_IMAGE_CONFIRM_DELAY_S);
}

void mcuhome_health_operational(void)
{
	if (confirm_armed) {
		/* Idempotent on purpose: a bring-up stage that runs twice
		 * must not be able to push confirmation out forever. */
		return;
	}
	confirm_armed = true;

	if (!booted_as_test_image) {
		LOG_DBG("running a confirmed image, no confirmation needed");
		return;
	}

	LOG_INF("running an UNCONFIRMED image — confirming in %u s if it stays healthy, "
		"otherwise the next boot reverts",
		CONFIG_MCUHOME_IMAGE_CONFIRM_DELAY_S);
	(void)k_work_schedule(&confirm_work, K_MSEC(CONFIRM_DELAY_MS));
}

bool mcuhome_health_image_pending(void)
{
	return booted_as_test_image && !boot_is_img_confirmed();
}

static int mcuhome_health_image_state(void)
{
	booted_as_test_image = !boot_is_img_confirmed();
	return 0;
}

/* Runs before the application can call anything above, and after the flash
 * driver the MCUboot trailer lives on is up. */
SYS_INIT(mcuhome_health_image_state, APPLICATION, CONFIG_APPLICATION_INIT_PRIORITY);
