/*
 * SPDX-FileCopyrightText: 2026 The MCUHome Contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * MCUHome's Matter OTA image processor (ota_image_processor.h).
 *
 * The download path, end to end:
 *
 *   PrepareDownload  open slot1 through the flash map, point stream_flash
 *                    at the part it actually lives on, reset the header
 *                    parser.
 *   ProcessBlock     feed the block to the header parser until the header
 *                    is complete, check vendor/product/size against this
 *                    device, then stream the payload into slot1.
 *   Finalize         flush the write buffer.
 *   Apply            boot_request_upgrade(BOOT_UPGRADE_TEST) and reboot.
 *
 * TEST, never PERMANENT. The whole rollback story of ADR 0015 hangs on the
 * image arriving as a test image that has to confirm itself
 * (lib/health/image_confirm.c); a permanent upgrade would install an
 * unverified image with no way back, over the air, on a device with no
 * cable attached.
 *
 * What this does NOT do is verify the payload. CHIP never checks the
 * Matter image digest on any platform, and MCUHome does not pretend to
 * either: MCUboot's signature over the image inside is the one and only
 * trust anchor (ADR 0015 decision 6). A payload that is not signed with
 * the user's key is written to slot1, refused by the bootloader on the
 * next boot, and the device comes back on the old image. That is the
 * intended failure mode, and it is why the checks below are about "is
 * this image *for this device*", not about "is this image genuine".
 */

#include <errno.h>

#include <zephyr/device.h>
#include <zephyr/devicetree.h>
#include <zephyr/dfu/mcuboot.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/storage/flash_map.h>
#include <zephyr/storage/stream_flash.h>
#include <zephyr/sys/reboot.h>
#include <zephyr/sys/util.h>

#include <app/clusters/ota-requestor/OTADownloader.h>
#include <app/clusters/ota-requestor/OTARequestorInterface.h>
#include <lib/support/CodeUtils.h>
#include <platform/CHIPDeviceLayer.h>

#include "matter_internal.h"
#include "ota_image_processor.h"

LOG_MODULE_DECLARE(MCUHOME_MATTER_LOG_MODULE, CONFIG_LOG_DEFAULT_LEVEL);

/* The staging slot is devicetree, not a constant: ADR 0015 decision 2
 * makes the partition table per-board registry data, and the builder
 * renders it into the board overlay. A board configured without one
 * cannot take an over-the-air update at all, and saying so here beats a
 * link error about a missing partition symbol. */
#if !DT_NODE_EXISTS(DT_NODELABEL(slot1_partition))
#error "CONFIG_MCUHOME_MATTER_OTA needs a slot1_partition in the board's devicetree. " \
	"Only board classes with a staging slot can take a Matter OTA update (ADR 0015 " \
	"decision 5); the update scheme in mcuhome/registry.py is what turns this on."
#endif

/* The Zephyr side of Matter OTA is one indivisible Kconfig group, emitted
 * by the board's update scheme in mcuhome/registry.py. It is NOT expressed
 * as Kconfig `select`s: MCUHOME_MATTER hangs off the libc choice, and
 * selecting FLASH, STREAM_FLASH or IMG_MANAGER from under it closes a
 * dependency cycle through REBOOT and USB_DFU_REBOOT that makes Kconfig
 * refuse to parse the tree — in every build in the repository, not only in
 * one with OTA on (components/matter/Kconfig says the same at more
 * length). The group is therefore checked here instead, by name, so that a
 * missing member is a message rather than a link error or, worse, silence. */
#if !defined(CONFIG_MCUBOOT_IMG_MANAGER)
#error "CONFIG_MCUHOME_MATTER_OTA needs CONFIG_IMG_MANAGER=y (which defaults to the MCUboot " \
	"image manager): boot_request_upgrade() and the image trailer come from it."
#endif

#if !defined(CONFIG_FLASH_MAP)
#error "CONFIG_MCUHOME_MATTER_OTA needs CONFIG_FLASH_MAP=y: the staging slot is looked up in the " \
	"flash map, which is what lets it live on a part other than the SoC's own flash."
#endif

#if defined(CONFIG_FLASH_HAS_EXPLICIT_ERASE) && !defined(CONFIG_STREAM_FLASH_ERASE)
#error "CONFIG_MCUHOME_MATTER_OTA needs CONFIG_STREAM_FLASH_ERASE=y on a part that has to be " \
	"erased before it is written. Without it stream_flash writes into pages nobody erased, " \
	"the staged image is silently wrong, and the failure only shows up after the download, " \
	"the reboot and the swap attempt."
#endif

#if !defined(CONFIG_REBOOT)
#error "CONFIG_MCUHOME_MATTER_OTA needs CONFIG_REBOOT: applying an update means rebooting into " \
	"the swapped image."
#endif

/* ADR 0015 is a test/confirm architecture. CHIP offers a Kconfig choice
 * that turns the upgrade permanent, which would silently remove the
 * rollback this whole block exists to provide. */
#ifdef CONFIG_CHIP_OTA_REQUEST_UPGRADE_PERMANENT
#error "CONFIG_CHIP_OTA_REQUEST_UPGRADE_PERMANENT gives up MCUboot's revert path, which " \
	"ADR 0015 makes the point of staging on this board class. Use " \
	"CONFIG_CHIP_OTA_REQUEST_UPGRADE_TEST (the default) and let the image confirm itself."
#endif

namespace mcuhome
{

namespace
{

/** Grace period between announcing the reboot and performing it. */
constexpr uint32_t kRebootDelayMs = 500;

OtaImageProcessor gImageProcessor;

} // namespace

OtaImageProcessor &OtaImageProcessor::Instance()
{
	return gImageProcessor;
}

CHIP_ERROR OtaImageProcessor::PrepareDownload()
{
	VerifyOrReturnError(mDownloader != nullptr, CHIP_ERROR_INCORRECT_STATE);

	/* Off the caller's thread and onto the CHIP event loop, which is
	 * where the downloader expects its completion. Same shape as CHIP's
	 * own processor. */
	return chip::DeviceLayer::SystemLayer().ScheduleLambda(
		[this] { (void)mDownloader->OnPreparedForDownload(PrepareDownloadImpl()); });
}

CHIP_ERROR OtaImageProcessor::PrepareDownloadImpl()
{
	const struct flash_area *area = nullptr;
	const struct device *flash = nullptr;
	int rc;

	mcuhome_ota_header_init(&mParser);
	mHeader = {};
	mParams = {};
	mWriting = false;

	/* The flash map is what makes this processor board-agnostic: it
	 * answers which device the staging slot is on, and at what offset
	 * within THAT device — the two facts CHIP's own processor assumes
	 * instead of asking. */
	rc = flash_area_open(PARTITION_ID(slot1_partition), &area);
	if (rc != 0) {
		LOG_ERR("staging slot is not in the flash map (%d)", rc);
		return chip::System::MapErrorZephyr(rc);
	}

	flash = flash_area_get_device(area);
	if (flash == nullptr || !device_is_ready(flash)) {
		LOG_ERR("staging flash device is not ready");
		flash_area_close(area);
		return chip::System::MapErrorZephyr(-ENODEV);
	}

	LOG_INF("staging slot: %s +0x%lx, %zu KiB", flash->name, (unsigned long)area->fa_off,
		area->fa_size / 1024U);

	rc = stream_flash_init(&mStream, flash, mBuffer, sizeof(mBuffer), (size_t)area->fa_off,
			       area->fa_size, nullptr);
	flash_area_close(area);
	if (rc != 0) {
		LOG_ERR("stream_flash_init failed (%d)", rc);
		return chip::System::MapErrorZephyr(rc);
	}

	mWriting = true;
	return CHIP_NO_ERROR;
}

CHIP_ERROR OtaImageProcessor::ConsumeHeader(chip::ByteSpan &block)
{
	const uint8_t *data = block.data();
	size_t length = block.size();
	int rc;

	if (!mcuhome_ota_header_pending(&mParser)) {
		return CHIP_NO_ERROR;
	}

	rc = mcuhome_ota_header_parse(&mParser, &data, &length, &mHeader);
	block = chip::ByteSpan(data, length);
	if (rc == -EAGAIN) {
		/* The header spans more than one block. Nothing to write
		 * yet, and nothing wrong. */
		return CHIP_NO_ERROR;
	}
	if (rc != 0) {
		LOG_ERR("malformed Matter OTA header (%d)", rc);
		return CHIP_ERROR_INVALID_FILE_IDENTIFIER;
	}

	LOG_INF("OTA image: vendor 0x%04x product 0x%04x version %u (%s), payload %llu B",
		mHeader.vendor_id, mHeader.product_id, mHeader.software_version,
		mHeader.software_version_string, (unsigned long long)mHeader.payload_size);

	/* Belt and braces over the provider's own matching. The requestor
	 * asks a provider for an image for this vendor/product, but nothing
	 * re-checks what actually arrives over BDX — and writing another
	 * product's image into slot1 costs a bootloader cycle and a revert
	 * for no reason. */
	if (mHeader.vendor_id != CONFIG_CHIP_DEVICE_VENDOR_ID ||
	    mHeader.product_id != CONFIG_CHIP_DEVICE_PRODUCT_ID) {
		LOG_ERR("image is for vendor 0x%04x product 0x%04x, this device is 0x%04x/0x%04x",
			mHeader.vendor_id, mHeader.product_id, CONFIG_CHIP_DEVICE_VENDOR_ID,
			CONFIG_CHIP_DEVICE_PRODUCT_ID);
		return CHIP_ERROR_INVALID_ARGUMENT;
	}

	/* The staging slot is finite and stream_flash would fail somewhere in
	 * the middle. Failing here costs the user one error message instead
	 * of a download. */
	if (mHeader.payload_size > (uint64_t)PARTITION_SIZE(slot1_partition)) {
		LOG_ERR("image payload is %llu B, the staging slot holds %u B",
			(unsigned long long)mHeader.payload_size,
			(unsigned int)PARTITION_SIZE(slot1_partition));
		return CHIP_ERROR_BUFFER_TOO_SMALL;
	}

	mParams.totalFileBytes = mHeader.payload_size;
	return CHIP_NO_ERROR;
}

CHIP_ERROR OtaImageProcessor::ProcessBlock(chip::ByteSpan &block)
{
	VerifyOrReturnError(mDownloader != nullptr, CHIP_ERROR_INCORRECT_STATE);
	VerifyOrReturnError(mWriting, CHIP_ERROR_INCORRECT_STATE);

	CHIP_ERROR error = ConsumeHeader(block);

	if (error == CHIP_NO_ERROR && !block.empty()) {
		int rc = stream_flash_buffered_write(&mStream, block.data(), block.size(), false);

		if (rc != 0) {
			LOG_ERR("writing %zu B to the staging slot failed (%d)", block.size(), rc);
			error = chip::System::MapErrorZephyr(rc);
		} else {
			mParams.downloadedBytes += block.size();
		}
	}

	/* The downloader is driven from the CHIP event loop, so the answer
	 * goes back through it rather than being returned up this call
	 * stack — which may be a BDX receive context. */
	return chip::DeviceLayer::SystemLayer().ScheduleLambda([this, error] {
		if (error == CHIP_NO_ERROR) {
			(void)mDownloader->FetchNextData();
		} else {
			mDownloader->EndDownload(error);
		}
	});
}

CHIP_ERROR OtaImageProcessor::Finalize()
{
	int rc = stream_flash_buffered_write(&mStream, nullptr, 0, true);

	mWriting = false;
	if (rc != 0) {
		LOG_ERR("flushing the staging slot failed (%d)", rc);
		return chip::System::MapErrorZephyr(rc);
	}
	LOG_INF("staged %zu B in the secondary slot", stream_flash_bytes_written(&mStream));
	return CHIP_NO_ERROR;
}

CHIP_ERROR OtaImageProcessor::Abort()
{
	/* Nothing to undo. A half-written slot1 is not dangerous: MCUboot
	 * only swaps a slot whose image is complete, signed and marked for
	 * upgrade, and the next download overwrites it from the start.
	 * Erasing it here would cost seconds of SPI-NOR erase for no gain. */
	LOG_WRN("OTA download aborted; the staging slot keeps a partial image until the next one");
	mWriting = false;
	return CHIP_NO_ERROR;
}

CHIP_ERROR OtaImageProcessor::Apply()
{
	int rc = boot_request_upgrade(BOOT_UPGRADE_TEST);

	if (rc != 0) {
		LOG_ERR("MCUboot refused the upgrade request (%d)", rc);
		return chip::System::MapErrorZephyr(rc);
	}

	LOG_INF("upgrade staged as a TEST image; rebooting to let MCUboot swap it in");
	chip::DeviceLayer::PlatformMgr().HandleServerShuttingDown();

	/* Delayed, so that the state-transition event reaches the controller
	 * before the radio goes away with the rest of the system. */
	return chip::DeviceLayer::SystemLayer().StartTimer(
		chip::System::Clock::Milliseconds32(kRebootDelayMs),
		[](chip::System::Layer *, void *) {
			k_msleep(CHIP_DEVICE_CONFIG_SERVER_SHUTDOWN_ACTIONS_SLEEP_MS);
			sys_reboot(SYS_REBOOT_WARM);
		},
		nullptr);
}

bool OtaImageProcessor::IsFirstImageRun()
{
	chip::OTARequestorInterface *requestor = chip::GetRequestorInstance();
	VerifyOrReturnError(requestor != nullptr, false);

	uint32_t version = 0;

	VerifyOrReturnError(chip::DeviceLayer::ConfigurationMgr().GetSoftwareVersion(version) ==
				    CHIP_NO_ERROR,
			    false);

	return requestor->GetCurrentUpdateState() ==
		       chip::OTARequestorInterface::OTAUpdateStateEnum::kApplying &&
	       requestor->GetTargetVersion() == version;
}

CHIP_ERROR OtaImageProcessor::ConfirmCurrentImage()
{
	/* Deliberately NOT boot_write_img_confirmed() here.
	 *
	 * CHIP's DefaultOTARequestorDriver::Init() calls this the moment it
	 * recognizes a first run of a new image — that is, during bring-up,
	 * before the node has proven anything. ADR 0015's health amendment
	 * asks for the opposite: confirm only after the device has been
	 * healthy for a while, so that an image which reaches the Matter
	 * stack and then faults still reverts. lib/health/image_confirm.c
	 * owns the trailer write and mcuhome_matter_start() arms it.
	 *
	 * Returning success here is honest at the Matter layer: the update
	 * HAS been applied and the node is running it. If the image then
	 * fails to stay healthy, the revert shows up to the controller as
	 * the version going back, which is exactly what happened. */
#ifdef CONFIG_MCUHOME_IMAGE_CONFIRM
	LOG_INF("confirmation deferred to the health timer (%u s of healthy uptime)",
		CONFIG_MCUHOME_IMAGE_CONFIRM_DELAY_S);
	return CHIP_NO_ERROR;
#else
	/* Without the health foundation nothing else would ever confirm, and
	 * an unconfirmed image reverts on the next boot — so a build that
	 * switched it off gets the immediate write CHIP expects. */
	return chip::System::MapErrorZephyr(boot_write_img_confirmed());
#endif
}

} // namespace mcuhome
