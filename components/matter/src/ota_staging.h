/*
 * SPDX-FileCopyrightText: 2026 The MCUHome Contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * Where a downloaded image is put so that MCUboot finds it.
 *
 * THE REGRESSION THIS MODULE EXISTS TO PREVENT
 *
 * MCUboot's swap-using-offset mode — the mode every MCUHome board class A
 * runs (ADR 0015 decision 3, mcuhome/registry.py) — does NOT expect the
 * update image at the start of the secondary slot. It expects it one
 * erase sector in. That sector is the working room the algorithm uses
 * instead of a scratch partition, and the bootloader hard-codes the
 * assumption:
 *
 *   bootloader/mcuboot/boot/bootutil/src/swap_offset.c:98-100
 *       boot_read_image_header(): for the secondary slot, and unless a
 *       revert is pending, the header is read at
 *       boot_img_sector_size(state, BOOT_SLOT_SECONDARY, 0).
 *   bootloader/mcuboot/boot/bootutil/src/bootutil_area.c:126-133
 *       "In case of swap offset, header of secondary slot image is
 *       positioned in second sector of slot."
 *   bootloader/mcuboot/boot/bootutil/src/loader.c:1046-1047
 *       the swap copies the secondary starting at
 *       boot_img_sector_size(..., BOOT_SLOT_SECONDARY, 0).
 *
 * An image written at offset 0 instead is not a loud failure. It is a
 * SILENT one, and MCUHome shipped it once: the trailer says "upgrade
 * pending", so boot_swap_type_multi() answers TEST; the bootloader then
 * finds no header where it looks, notices the header sitting in the first
 * sector, logs
 *
 *     "Secondary header magic detected in first sector, wrong upload
 *      address?"                     (loader.c:595-611, message on 608)
 *
 * and refuses the swap (FIH_NO_BOOTABLE_IMAGE -> BOOT_SWAP_TYPE_NONE at
 * loader.c:739-746), erasing the staged image on the way out
 * (boot_scramble_slot(), loader.c:653). The device boots the old image.
 * With the bootloader's logging off — which is the shipping configuration
 * on a board whose console is taken by serial recovery — the ONLY
 * observable symptom is the controller reporting the old software version
 * after a download that reported success. That is what happened on the
 * bench: two apply attempts, no bootloader complaint, slot0 still 0.1.0.
 *
 * SO THE OFFSET IS NEVER COMPUTED HERE
 *
 * Zephyr owns this rule and applies it in exactly one place —
 * flash_img_init_id() (zephyr/subsys/dfu/img_util/flash_img.c:196-252),
 * which is the writer Zephyr's own mcumgr img_mgmt uses for every DFU
 * upload (zephyr/subsys/mgmt/mcumgr/grp/img_mgmt/src/zephyr_img_mgmt.c:
 * 524-539). It queries the first sector of the upgrade slot, erases it,
 * and points the stream at the sector after it. This module is a thin
 * shell around that call, and it is thin ON PURPOSE: a hand-computed
 * offset here would be a second copy of a rule that changes with the
 * MCUboot mode, and the copy that drifts is invisible until an OTA
 * silently does nothing.
 *
 * It is its own translation unit, free of CHIP, so that
 * tests/ota_staging can drive it on native_sim and assert that the
 * payload lands one sector into the slot in swap-using-offset mode and at
 * offset 0 in a mode without one. The same reason ota_image_header.c is
 * its own translation unit (ADR 0014).
 */

#ifndef MCUHOME_COMPONENTS_MATTER_OTA_STAGING_H_
#define MCUHOME_COMPONENTS_MATTER_OTA_STAGING_H_

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include <zephyr/dfu/flash_img.h>

#ifdef __cplusplus
extern "C" {
#endif

/**
 * A download in progress, writing into one flash area.
 *
 * Statically allocated by the caller; the write-through buffer inside
 * @ref flash_img_context is sized by CONFIG_IMG_BLOCK_BUF_SIZE, which the
 * board's update scheme emits together with the rest of the DFU group
 * (mcuhome/registry.py).
 */
struct mcuhome_ota_staging {
	struct flash_img_context ctx;
	/** True between a successful open and finish/abort. */
	bool open;
};

/**
 * How much of the end of the staging slot is erased before a download
 * starts, rounded up to whole erase sectors.
 *
 * This has to cover MCUboot's image trailer, whose exact size the
 * application deliberately cannot compute: boot_trailer_sz() lives in
 * bootutil_area.h, an internal bootloader header, and re-deriving it here
 * would be a second copy of a number that changes with the swap mode —
 * the mistake this whole module exists to avoid. So the value below is an
 * upper bound with its derivation written down instead:
 *
 *   trailer = status area + info fields
 *   status area = BOOT_MAX_IMG_SECTORS * BOOT_STATUS_STATE_COUNT (3)
 *                 * the slot's write-block size
 *   info fields = magic (16) + swap_type/copy_done/image_ok/swap_size
 *                 (4 * BOOT_MAX_ALIGN) + one more BOOT_MAX_ALIGN for
 *                 swap-using-offset's TLV size = well under 128 B
 *
 * For an MCUHome class-A board — 912 KiB of slot in 4 KiB sectors, so 228
 * sectors, on an SPI-NOR part with a write-block size of 1 — that is
 * 228 * 3 * 1 + ~56 = under 1 KiB. 8 KiB still covers a slot four times
 * that size on a part with a 4-byte write block. Two sector erases on the
 * parts MCUHome uses, paid once at PrepareDownload.
 */
#define MCUHOME_OTA_STAGING_TRAILER_SZ 8192U

/**
 * Open @p area_id for staging and position the write cursor where the
 * bootloader will look for the image header.
 *
 * Erases two regions before the first byte is written:
 *
 *  - the sector the swap-using-offset rule skips (done by Zephyr's
 *    flash_img_init_id), so that a header left there by an earlier,
 *    wrongly placed download cannot make the bootloader refuse the new
 *    one, and
 *  - the last @ref MCUHOME_OTA_STAGING_TRAILER_SZ of the slot, which is
 *    where MCUboot keeps the swap status. flash_img_init_id() flattens
 *    sector 0 and nothing else, and the progressive erase that follows
 *    only ever reaches as far as the image is long — so on a slot larger
 *    than the image (912 KiB against ~690 KiB here) the swap-status bytes
 *    of a PREVIOUS, failed attempt survive underneath a fresh
 *    "upgrade pending" magic, and MCUboot's interrupted-swap resume logic
 *    reads them as its own unfinished work. Erasing them costs two
 *    sectors and removes a class of failure that would only ever show up
 *    as a bricked swap on somebody's shelf.
 *
 * @param staging  Caller-owned context.
 * @param area_id  Flash-area ID of the staging slot, e.g.
 *                 PARTITION_ID(slot1_partition).
 *
 * @return 0 on success, negative errno on failure.
 */
int mcuhome_ota_staging_open(struct mcuhome_ota_staging *staging, uint8_t area_id);

/**
 * Append @p len bytes to the staged image.
 *
 * @return 0 on success, negative errno on failure (-EFBIG once the slot
 *         is full, -EINVAL if the staging context is not open).
 */
int mcuhome_ota_staging_write(struct mcuhome_ota_staging *staging, const void *data, size_t len);

/**
 * Flush the write buffer and close the flash area.
 *
 * @return 0 on success, negative errno on failure.
 */
int mcuhome_ota_staging_finish(struct mcuhome_ota_staging *staging);

/**
 * Give up on a download in progress and release the flash area.
 *
 * Leaves whatever was written in place: an incomplete image fails
 * MCUboot's signature check and the next download overwrites it from the
 * start, so erasing here would only cost seconds of SPI-NOR erase.
 */
void mcuhome_ota_staging_abort(struct mcuhome_ota_staging *staging);

/** Payload bytes handed to @ref mcuhome_ota_staging_write so far. */
size_t mcuhome_ota_staging_written(struct mcuhome_ota_staging *staging);

#ifdef __cplusplus
}
#endif

#endif /* MCUHOME_COMPONENTS_MATTER_OTA_STAGING_H_ */
