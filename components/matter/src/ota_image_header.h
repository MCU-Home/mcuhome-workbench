/*
 * SPDX-FileCopyrightText: 2026 The MCUHome Contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * Streaming parser for the Matter OTA image header.
 *
 * A Matter OTA file is a 16-byte fixed header (magic, total size, header
 * TLV size), a Matter-TLV header structure, and then an opaque payload —
 * for MCUHome, the MCUboot-signed application image, byte for byte.
 * Everything the device needs in order to decide whether it wants the
 * payload is in the header; everything that follows goes straight to
 * flash.
 *
 * WHY THIS EXISTS RATHER THAN CHIP'S OTAImageHeaderParser
 *
 * Three reasons, in order of weight:
 *
 * 1. It is CHIP-free, so it is exercised on the host by
 *    tests/ota_image_header/ — the same pattern as table_validate.c
 *    (ADR 0014), and for the same reason: CHIP cannot build on
 *    native_sim, and a parser that decides what gets written into the
 *    other half of the device's flash deserves exhaustive tests.
 * 2. It has no heap. CHIP's parser allocates the TLV header through
 *    Platform::ScopedMemoryBuffer; MCUHome's coding standard is static
 *    allocation, and the bound here is a compile-time constant.
 * 3. It reports the vendor and product IDs, which the caller checks
 *    against the running image's own before writing a single byte. CHIP's
 *    requestor checks those on the *provider* side of the query; the
 *    payload that then arrives over BDX is never re-checked, and nothing
 *    else in the path does either — CHIP never verifies the image digest
 *    on any platform, so this and MCUboot's signature are the whole of
 *    the payload's defence.
 *
 * Everything here is pure: no Zephyr, no CHIP, no allocation, no I/O.
 */

#ifndef MCUHOME_COMPONENTS_MATTER_OTA_IMAGE_HEADER_H_
#define MCUHOME_COMPONENTS_MATTER_OTA_IMAGE_HEADER_H_

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/** File signature of a Matter OTA image (Matter 1.4 §11.21.1). */
#define MCUHOME_OTA_MAGIC 0x1BEEF11EU

/** Size of the fixed part that precedes the header TLV. */
#define MCUHOME_OTA_FIXED_HEADER_SIZE 16U

/**
 * Largest header TLV this parser accepts, in bytes.
 *
 * The mandatory fields need about 50; the optional ones are bounded by
 * the specification at 64 characters of version string and 256 of release
 * notes URL, so 512 covers every legal header with room to spare. A
 * larger one is refused rather than truncated — a header that does not
 * fit is a header this build does not understand.
 */
#ifndef MCUHOME_OTA_HEADER_MAX
#ifdef CONFIG_MCUHOME_MATTER_OTA_HEADER_MAX
#define MCUHOME_OTA_HEADER_MAX CONFIG_MCUHOME_MATTER_OTA_HEADER_MAX
#else
#define MCUHOME_OTA_HEADER_MAX 512U
#endif
#endif

/** Longest software version string kept, excluding the terminator. */
#define MCUHOME_OTA_VERSION_STRING_MAX 64U

/* Bits of mcuhome_ota_header::present. Plain literals rather than BIT():
 * <zephyr/sys/util.h> has no business in a translation unit that is meant
 * to compile anywhere. */
#define MCUHOME_OTA_HDR_VENDOR_ID        0x01U
#define MCUHOME_OTA_HDR_PRODUCT_ID       0x02U
#define MCUHOME_OTA_HDR_SOFTWARE_VERSION 0x04U
#define MCUHOME_OTA_HDR_PAYLOAD_SIZE     0x08U

/** The four fields a payload cannot be accepted without. */
#define MCUHOME_OTA_HDR_REQUIRED                                                                   \
	(MCUHOME_OTA_HDR_VENDOR_ID | MCUHOME_OTA_HDR_PRODUCT_ID |                                  \
	 MCUHOME_OTA_HDR_SOFTWARE_VERSION | MCUHOME_OTA_HDR_PAYLOAD_SIZE)

/** Decoded header. Only the fields MCUHome acts on are kept. */
struct mcuhome_ota_header {
	/** Size of the whole file, header included. */
	uint64_t total_size;
	/** Size of the payload that follows the header. */
	uint64_t payload_size;
	/** Size of the header TLV, from the fixed part. */
	uint32_t header_tlv_size;
	uint32_t software_version;
	uint16_t vendor_id;
	uint16_t product_id;
	/** Always NUL-terminated; empty when the header carried none. */
	char software_version_string[MCUHOME_OTA_VERSION_STRING_MAX + 1U];
	/** Which of the required fields were present (MCUHOME_OTA_HDR_*). */
	uint8_t present;
};

/** Parser state. Zero-initialize through mcuhome_ota_header_init(). */
struct mcuhome_ota_header_parser {
	uint32_t needed;
	uint32_t fill;
	uint8_t state;
	uint8_t buffer[MCUHOME_OTA_HEADER_MAX];
};

/** Reset @p parser to expect the start of a new image. */
void mcuhome_ota_header_init(struct mcuhome_ota_header_parser *parser);

/** True while @p parser still expects header bytes. */
bool mcuhome_ota_header_pending(const struct mcuhome_ota_header_parser *parser);

/**
 * @brief Feed one block of the image to the header parser.
 *
 * Consumes as much of the block as belongs to the header and advances
 * @p data / @p len past it, so the caller can hand whatever is left
 * straight to flash without knowing where the boundary was.
 *
 * @param parser Parser state.
 * @param data   In/out: start of the unconsumed block.
 * @param len    In/out: bytes remaining in the block.
 * @param out    Filled in when the return value is 0. Untouched otherwise.
 *
 * @retval 0 The header is complete; @p data and @p len now describe the
 *           first payload bytes of the block (possibly none).
 * @retval -EAGAIN More data is needed. The whole block was consumed.
 * @retval -EILSEQ Not a Matter OTA image, or the header TLV is malformed
 *                 or missing a required field.
 * @retval -ENOSPC The header is larger than MCUHOME_OTA_HEADER_MAX.
 * @retval -EINVAL A NULL argument.
 */
int mcuhome_ota_header_parse(struct mcuhome_ota_header_parser *parser, const uint8_t **data,
			     size_t *len, struct mcuhome_ota_header *out);

#ifdef __cplusplus
}
#endif

#endif /* MCUHOME_COMPONENTS_MATTER_OTA_IMAGE_HEADER_H_ */
