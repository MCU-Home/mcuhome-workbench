/*
 * SPDX-FileCopyrightText: 2026 The MCUHome Contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * Staging-slot writer — see ota_staging.h for why the offset is Zephyr's
 * business and not this file's.
 */

#include <errno.h>

#include <zephyr/dfu/flash_img.h>
#include <zephyr/storage/flash_map.h>

#include "ota_staging.h"

int mcuhome_ota_staging_open(struct mcuhome_ota_staging *staging, uint8_t area_id)
{
	int rc;

	if (staging == NULL) {
		return -EINVAL;
	}

	staging->open = false;

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
