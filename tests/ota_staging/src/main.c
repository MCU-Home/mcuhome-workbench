/*
 * SPDX-FileCopyrightText: 2026 The MCUHome Contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * Where does a downloaded image actually land in the staging slot?
 *
 * Every test here answers that by reading the slot back through the
 * flash map and looking, rather than by asking the code under test where
 * it thinks it put things. The expected position is derived from the
 * devicetree and the bootloader mode this image was built for — never
 * from ota_staging.c — so a writer that stopped following the mode fails
 * here instead of on a device that quietly keeps booting its old image.
 */

#include <string.h>

#include <zephyr/devicetree.h>
#include <zephyr/kernel.h>
#include <zephyr/storage/flash_map.h>
#include <zephyr/ztest.h>

#include "ota_staging.h"

#define STAGING_AREA_ID PARTITION_ID(slot1_partition)
#define STAGING_SIZE    PARTITION_SIZE(slot1_partition)

/* The slot's erase unit, straight from the flash node the partition sits
 * on — the same number MCUboot's flash map derives its sector layout
 * from. native_sim's simulated flash uses 4 KiB. */
#define SLOT_SECTOR_SIZE DT_PROP(DT_GPARENT(DT_NODELABEL(slot1_partition)), erase_block_size)

/*
 * What the bootloader this image was built for expects.
 *
 * Spelled out here rather than imported from anywhere, because a test
 * that got its expectation from the same place as the implementation
 * would agree with the implementation by construction. The rule is
 * MCUboot's, quoted from bootutil_area.c:126-133: "In case of swap
 * offset, header of secondary slot image is positioned in second sector
 * of slot."
 */
#if defined(CONFIG_MCUBOOT_BOOTLOADER_MODE_SWAP_USING_OFFSET)
#define EXPECTED_IMAGE_OFFSET SLOT_SECTOR_SIZE
#else
#define EXPECTED_IMAGE_OFFSET 0U
#endif

/** MCUboot's image header magic, little-endian on the wire. */
static const uint8_t kImageMagic[4] = {0x3d, 0xb8, 0xf3, 0x96};

/** Stand-in for a signed image: recognizable, longer than one buffer. */
#define PAYLOAD_SIZE 3000U
static uint8_t payload[PAYLOAD_SIZE];
static uint8_t readback[PAYLOAD_SIZE];

static struct mcuhome_ota_staging staging;

static void fill_payload(void)
{
	memcpy(payload, kImageMagic, sizeof(kImageMagic));
	for (size_t i = sizeof(kImageMagic); i < PAYLOAD_SIZE; i++) {
		payload[i] = (uint8_t)(i * 7U + 13U);
	}
}

static void slot_read(size_t off, void *dst, size_t len)
{
	const struct flash_area *area = NULL;

	zassert_ok(flash_area_open(STAGING_AREA_ID, &area), "staging slot not in the flash map");
	zassert_ok(flash_area_read(area, off, dst, len), "reading back the staging slot failed");
	flash_area_close(area);
}

static bool slot_range_is_erased(size_t off, size_t len)
{
	const struct flash_area *area = NULL;
	uint8_t erased;
	uint8_t byte;
	bool all = true;

	zassert_ok(flash_area_open(STAGING_AREA_ID, &area), "staging slot not in the flash map");
	erased = flash_area_erased_val(area);

	for (size_t i = 0; i < len && all; i++) {
		zassert_ok(flash_area_read(area, off + i, &byte, 1), "read failed");
		all = (byte == erased);
	}

	flash_area_close(area);
	return all;
}

/** Put the whole slot back to its erased state between tests. */
static void slot_erase(void)
{
	const struct flash_area *area = NULL;

	zassert_ok(flash_area_open(STAGING_AREA_ID, &area), "staging slot not in the flash map");
	zassert_ok(flash_area_flatten(area, 0, STAGING_SIZE), "erasing the staging slot failed");
	flash_area_close(area);
}

static void stage_payload(void)
{
	zassert_ok(mcuhome_ota_staging_open(&staging, STAGING_AREA_ID), "open failed");
	zassert_ok(mcuhome_ota_staging_write(&staging, payload, PAYLOAD_SIZE), "write failed");
	zassert_ok(mcuhome_ota_staging_finish(&staging), "finish failed");
}

static void *suite_setup(void)
{
	fill_payload();
	return NULL;
}

static void test_before(void *fixture)
{
	ARG_UNUSED(fixture);
	memset(&staging, 0, sizeof(staging));
	memset(readback, 0, sizeof(readback));
	slot_erase();
}

ZTEST_SUITE(mcuhome_ota_staging, NULL, suite_setup, test_before, NULL, NULL);

/*
 * THE regression test. The image has to start exactly where the
 * bootloader reads its header from, byte for byte.
 */
ZTEST(mcuhome_ota_staging, test_payload_lands_at_the_offset_mcuboot_reads)
{
	stage_payload();

	slot_read(EXPECTED_IMAGE_OFFSET, readback, PAYLOAD_SIZE);
	zassert_mem_equal(readback, payload, PAYLOAD_SIZE,
			  "the staged image is not at slot+0x%x, which is where this image's "
			  "bootloader mode looks for its header",
			  (unsigned int)EXPECTED_IMAGE_OFFSET);
}

/*
 * The failure mode that shipped: an image at offset 0. MCUboot spots the
 * header magic in the first sector and refuses the swap
 * (loader.c:595-612), so nothing may leave a header there.
 */
ZTEST(mcuhome_ota_staging, test_no_image_header_is_left_in_the_first_sector)
{
	uint8_t first[sizeof(kImageMagic)];

	stage_payload();
	slot_read(0, first, sizeof(first));

	if (EXPECTED_IMAGE_OFFSET == 0U) {
		/* In a mode without an offset, offset 0 is where the
		 * image belongs and the magic being there is correct. */
		zassert_mem_equal(first, kImageMagic, sizeof(first),
				  "the image should start at the slot's first byte here");
		ztest_test_skip();
	}

	zassert_true(memcmp(first, kImageMagic, sizeof(first)) != 0,
		     "an image header at the slot's first byte is exactly what makes MCUboot "
		     "refuse the update without swapping");
	zassert_true(slot_range_is_erased(0, EXPECTED_IMAGE_OFFSET),
		     "the sector the offset skips must be left erased");
}

/*
 * A slot that already holds a wrongly placed image from an older, buggy
 * build must not poison the next download: MCUboot's first-sector check
 * fires on whatever is there, not on what this download wrote.
 */
ZTEST(mcuhome_ota_staging, test_a_stale_first_sector_header_is_cleared)
{
	const struct flash_area *area = NULL;
	uint8_t first[sizeof(kImageMagic)];

	if (EXPECTED_IMAGE_OFFSET == 0U) {
		ztest_test_skip();
	}

	/* Simulate the old bug's leftovers. */
	zassert_ok(flash_area_open(STAGING_AREA_ID, &area), "staging slot not in the flash map");
	zassert_ok(flash_area_write(area, 0, kImageMagic, sizeof(kImageMagic)), "write failed");
	flash_area_close(area);

	stage_payload();

	slot_read(0, first, sizeof(first));
	zassert_true(memcmp(first, kImageMagic, sizeof(first)) != 0,
		     "opening the slot for staging has to clear the sector the offset skips");
}

/*
 * The trailer region — the end of the slot, where MCUboot keeps the swap
 * status — has to be blank before a download starts.
 *
 * flash_img_init_id() flattens sector 0 and nothing else, and the
 * progressive erase behind stream_flash only reaches as far as the image
 * is long. So on any slot bigger than the image (912 KiB against ~690 KiB
 * on the reference board) the status bytes of a previous, failed attempt
 * outlive it, sitting under a fresh "upgrade pending" magic where
 * MCUboot's interrupted-swap resume logic will read them as its own
 * unfinished work.
 *
 * The test writes a recognizable pattern over the last sectors, stages an
 * image that comes nowhere near them, and asserts the pattern is gone.
 */
ZTEST(mcuhome_ota_staging, test_the_trailer_region_is_erased_before_a_download)
{
	const size_t trailer_off = (size_t)STAGING_SIZE - MCUHOME_OTA_STAGING_TRAILER_SZ;
	static uint8_t stale[MCUHOME_OTA_STAGING_TRAILER_SZ];
	const struct flash_area *area = NULL;

	BUILD_ASSERT(MCUHOME_OTA_STAGING_TRAILER_SZ < STAGING_SIZE,
		     "the trailer region has to be a tail of the slot, not the whole thing");

	for (size_t i = 0; i < sizeof(stale); i++) {
		stale[i] = (uint8_t)(i ^ 0x5AU);
	}

	zassert_ok(flash_area_open(STAGING_AREA_ID, &area), "staging slot not in the flash map");
	zassert_ok(flash_area_write(area, trailer_off, stale, sizeof(stale)), "write failed");
	flash_area_close(area);

	stage_payload();

	zassert_true(slot_range_is_erased(trailer_off, MCUHOME_OTA_STAGING_TRAILER_SZ),
		     "opening the slot for staging has to erase the swap-status region, or a "
		     "previous failed attempt's status survives under the new image");
}

/*
 * ...and erasing it must not cost the image anything: the two regions the
 * writer clears are at opposite ends of the slot, and the payload in
 * between has to read back exactly.
 */
ZTEST(mcuhome_ota_staging, test_erasing_the_trailer_leaves_the_image_alone)
{
	const struct flash_area *area = NULL;

	zassert_ok(flash_area_open(STAGING_AREA_ID, &area), "staging slot not in the flash map");
	zassert_ok(flash_area_write(area, (size_t)STAGING_SIZE - 4U, kImageMagic, 4U),
		   "write failed");
	flash_area_close(area);

	stage_payload();

	slot_read(EXPECTED_IMAGE_OFFSET, readback, PAYLOAD_SIZE);
	zassert_mem_equal(readback, payload, PAYLOAD_SIZE,
			  "clearing the trailer must not disturb the staged image");
}

/* The offset is the writer's business, not the caller's: what the caller
 * counts is payload. */
ZTEST(mcuhome_ota_staging, test_bytes_written_counts_payload_only)
{
	stage_payload();

	zassert_equal(mcuhome_ota_staging_written(&staging), PAYLOAD_SIZE,
		      "bytes written should not include whatever the slot layout reserves");
}

/* Several blocks, as BDX delivers them, must be contiguous in the slot. */
ZTEST(mcuhome_ota_staging, test_blocks_are_concatenated)
{
	const size_t split = 1500U;

	zassert_ok(mcuhome_ota_staging_open(&staging, STAGING_AREA_ID), "open failed");
	zassert_ok(mcuhome_ota_staging_write(&staging, payload, split), "write failed");
	zassert_ok(mcuhome_ota_staging_write(&staging, payload + split, PAYLOAD_SIZE - split),
		   "write failed");
	zassert_ok(mcuhome_ota_staging_finish(&staging), "finish failed");

	slot_read(EXPECTED_IMAGE_OFFSET, readback, PAYLOAD_SIZE);
	zassert_mem_equal(readback, payload, PAYLOAD_SIZE, "blocks were not written back to back");
}

/* An image that does not fit has to be refused, not wrapped or
 * truncated. The ceiling is the slot minus whatever the mode reserves,
 * which is why the caller cannot check it against the partition size
 * alone. */
ZTEST(mcuhome_ota_staging, test_overlong_image_is_refused)
{
	size_t remaining = (size_t)STAGING_SIZE + 1U;
	int rc = 0;

	zassert_ok(mcuhome_ota_staging_open(&staging, STAGING_AREA_ID), "open failed");

	while (remaining > 0U && rc == 0) {
		size_t chunk = MIN(remaining, (size_t)PAYLOAD_SIZE);

		rc = mcuhome_ota_staging_write(&staging, payload, chunk);
		remaining -= chunk;
	}

	zassert_not_equal(rc, 0,
			  "more than the whole partition has to be refused, never wrapped "
			  "or written past the slot");
	mcuhome_ota_staging_abort(&staging);
}

/* Nothing may touch flash through a context that was never opened, or
 * one that is already finished. */
ZTEST(mcuhome_ota_staging, test_writes_outside_an_open_download_are_refused)
{
	zassert_equal(mcuhome_ota_staging_write(&staging, payload, PAYLOAD_SIZE), -EINVAL,
		      "a write before open must be refused");
	zassert_equal(mcuhome_ota_staging_finish(&staging), -EINVAL,
		      "a finish before open must be refused");

	stage_payload();

	zassert_equal(mcuhome_ota_staging_write(&staging, payload, PAYLOAD_SIZE), -EINVAL,
		      "a write after finish must be refused");
}

/* Abort has to give the flash area back, or a second download cannot
 * open it — the path a cancelled or timed-out BDX transfer takes. */
ZTEST(mcuhome_ota_staging, test_a_download_can_follow_an_aborted_one)
{
	zassert_ok(mcuhome_ota_staging_open(&staging, STAGING_AREA_ID), "open failed");
	zassert_ok(mcuhome_ota_staging_write(&staging, payload, 512U), "write failed");
	mcuhome_ota_staging_abort(&staging);

	stage_payload();

	slot_read(EXPECTED_IMAGE_OFFSET, readback, PAYLOAD_SIZE);
	zassert_mem_equal(readback, payload, PAYLOAD_SIZE,
			  "a download after an aborted one has to land in the same place");
}
