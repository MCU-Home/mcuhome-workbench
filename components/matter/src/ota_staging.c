/*
 * SPDX-FileCopyrightText: 2026 The MCUHome Contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * Staging-slot writer — see ota_staging.h for why the offset is Zephyr's
 * business and not this file's.
 */

#include <errno.h>

#include <zephyr/devicetree.h>
#include <zephyr/dfu/flash_img.h>
#include <zephyr/drivers/flash.h>
#include <zephyr/storage/flash_map.h>

#include "ota_staging.h"

/*
 * Erase the tail of the slot, walking the page layout backwards from the
 * end so that no erase-unit size is assumed anywhere in this file. The
 * whole point of ota_staging is that geometry comes from Zephyr; a
 * hard-coded 4096 here would be the same mistake in a different place.
 *
 * A failure is returned, not swallowed: staging into a slot whose trailer
 * still holds a previous attempt's swap status is exactly the situation
 * this call exists to prevent, so failing the download now beats a
 * bootloader resuming somebody else's swap later.
 */
static int erase_trailer(uint8_t area_id)
{
	const struct flash_area *area = NULL;
	const struct device *flash;
	off_t region_start;
	size_t covered = 0U;
	int rc;

	rc = flash_area_open(area_id, &area);
	if (rc != 0) {
		return rc;
	}

	flash = flash_area_get_device(area);
	if (flash == NULL) {
		flash_area_close(area);
		return -ENODEV;
	}

	region_start = (off_t)area->fa_size;

	while (covered < MCUHOME_OTA_STAGING_TRAILER_SZ && region_start > 0) {
		struct flash_pages_info page;

		rc = flash_get_page_info_by_offs(flash, area->fa_off + region_start - 1, &page);
		if (rc != 0) {
			break;
		}

		region_start = (off_t)page.start_offset - (off_t)area->fa_off;
		if (region_start < 0) {
			/* A page that starts before the area does means the
			 * partition is not erase-unit aligned, which is a
			 * board-description error rather than something to
			 * paper over at runtime. */
			rc = -EINVAL;
			break;
		}
		covered += page.size;
	}

	if (rc == 0 && covered > 0U) {
		rc = flash_area_flatten(area, region_start, covered);
	}

	flash_area_close(area);
	return rc;
}

int mcuhome_ota_staging_open(struct mcuhome_ota_staging *staging, uint8_t area_id)
{
	int rc;

	if (staging == NULL) {
		return -EINVAL;
	}

	staging->open = false;

	/* Before the writer touches the slot: clear the swap status a
	 * previous failed attempt may have left at the end of it
	 * (ota_staging.h explains why flash_img_init_id() cannot). */
	rc = erase_trailer(area_id);
	if (rc != 0) {
		return rc;
	}

	/* THE one line this module is about. flash_img_init_id() opens the
	 * area, looks up its first sector, erases that sector and starts
	 * the stream after it whenever the image is built for
	 * swap-using-offset — and starts it at 0 in every other mode. Do
	 * not replace this with stream_flash_init() on the area's own
	 * offset; that is precisely the bug ota_staging.h describes. */
	rc = flash_img_init_id(&staging->ctx, area_id);
	if (rc != 0) {
		return rc;
	}

	staging->open = true;
	return 0;
}

int mcuhome_ota_staging_write(struct mcuhome_ota_staging *staging, const void *data, size_t len)
{
	if (staging == NULL || !staging->open) {
		return -EINVAL;
	}

	return flash_img_buffered_write(&staging->ctx, (const uint8_t *)data, len, false);
}

int mcuhome_ota_staging_finish(struct mcuhome_ota_staging *staging)
{
	int rc;

	if (staging == NULL || !staging->open) {
		return -EINVAL;
	}

	/* The flushing call also closes the flash area, whether or not it
	 * succeeds, so the context is spent either way. */
	rc = flash_img_buffered_write(&staging->ctx, NULL, 0, true);
	staging->open = false;
	return rc;
}

void mcuhome_ota_staging_abort(struct mcuhome_ota_staging *staging)
{
	if (staging == NULL || !staging->open) {
		return;
	}

	/* Only the flushing write closes the area; an abort has to. */
	if (staging->ctx.flash_area != NULL) {
		flash_area_close(staging->ctx.flash_area);
		staging->ctx.flash_area = NULL;
	}
	staging->open = false;
}

size_t mcuhome_ota_staging_written(struct mcuhome_ota_staging *staging)
{
	if (staging == NULL) {
		return 0;
	}

	return flash_img_bytes_written(&staging->ctx);
}
