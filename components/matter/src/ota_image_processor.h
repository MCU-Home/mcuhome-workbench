/*
 * SPDX-FileCopyrightText: 2026 The MCUHome Contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * MCUHome's Matter OTA image processor — framework-internal.
 *
 * CHIP's own chip::OTAImageProcessorImpl (src/platform/Zephyr) cannot
 * drive an MCUHome class-A board: it opens DT_CHOSEN(zephyr_flash_
 * controller) — the SoC's internal flash — and applies the slot1_partition
 * offsets to it. On the nRF7002-DK slot1 is on the MX25R64 over SPI4
 * (ADR 0015 decision 3), so writing "slot1's offset" into internal flash
 * would land in the middle of slot0 and the storage partition. Hence a
 * processor of MCUHome's own; upstream's stays compiled (CHIP's BUILD.gn
 * builds it whenever the requestor is on) and simply unused.
 */

#ifndef MCUHOME_COMPONENTS_MATTER_OTA_IMAGE_PROCESSOR_H_
#define MCUHOME_COMPONENTS_MATTER_OTA_IMAGE_PROCESSOR_H_

#include <lib/support/Span.h>
#include <platform/OTAImageProcessor.h>

#include "ota_image_header.h"
#include "ota_staging.h"

namespace chip
{
class OTADownloader;
}

namespace mcuhome
{

/**
 * Writes a downloaded Matter OTA payload into the board's staging slot and
 * hands the swap to MCUboot.
 *
 * Everything is statically allocated: the write-through buffer (inside
 * the staging context, sized by CONFIG_IMG_BLOCK_BUF_SIZE), the header
 * parser and the single instance itself. Nothing here allocates, and
 * nothing here blocks for longer than one flash page write.
 */
class OtaImageProcessor: public chip::OTAImageProcessorInterface
{
      public:
	/** The one instance. Framework-owned; there is no second slot. */
	static OtaImageProcessor &Instance();

	void SetDownloader(chip::OTADownloader *downloader)
	{
		mDownloader = downloader;
	}

	CHIP_ERROR PrepareDownload() override;
	CHIP_ERROR Finalize() override;
	CHIP_ERROR Abort() override;
	CHIP_ERROR Apply() override;
	CHIP_ERROR ProcessBlock(chip::ByteSpan &block) override;
	bool IsFirstImageRun() override;
	CHIP_ERROR ConfirmCurrentImage() override;

      private:
	CHIP_ERROR PrepareDownloadImpl();
	/** Consume whatever of @p block belongs to the OTA header. */
	CHIP_ERROR ConsumeHeader(chip::ByteSpan &block);

	/** Post-download check that the image is where MCUboot will look. */
	CHIP_ERROR VerifyStagedImage();

	chip::OTADownloader *mDownloader = nullptr;
	struct mcuhome_ota_header_parser mParser;
	struct mcuhome_ota_header mHeader;
	/* Where the bytes go, and — the whole point — WHERE in the slot.
	 * ota_staging.h carries the regression note. */
	struct mcuhome_ota_staging mStaging;
	bool mWriting = false;
};

} // namespace mcuhome

#endif /* MCUHOME_COMPONENTS_MATTER_OTA_IMAGE_PROCESSOR_H_ */
