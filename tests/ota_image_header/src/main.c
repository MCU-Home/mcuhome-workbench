/*
 * SPDX-FileCopyrightText: 2026 The MCUHome Contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * Unit tests for the Matter OTA image header parser
 * (components/matter/src/ota_image_header.c).
 *
 * Two kinds of input, on purpose:
 *
 * - Two GOLDEN headers, captured byte for byte from CHIP's own
 *   src/app/ota_image_tool.py — the tool the MCUHome builder drives. They
 *   are what proves the parser reads what the builder writes, including
 *   the compact TLV integer widths the tool picks (a payload size of 4096
 *   arrives as a two-byte unsigned, not a four-byte one) and the optional
 *   fields it can add.
 * - HAND-BUILT headers, for everything a well-behaved tool never emits:
 *   truncation, a wrong magic, a missing mandatory field, an oversized
 *   header, a nested container, a length that runs past the end.
 *
 * The block-splitting tests matter as much as the content ones: over BDX
 * the header arrives in whatever pieces the transfer happens to use, and
 * the boundary between "still header" and "already payload" can fall
 * anywhere — including inside the four bytes of the magic.
 */

#include <errno.h>
#include <string.h>

#include <zephyr/ztest.h>

#include "ota_image_header.h"

/* --- Golden headers ----------------------------------------------------- */

/* ota_image_tool.py create -v 0xFFF1 -p 0x8000 -vn 65536 -vs "1.0.0"
 *                          -da sha256 <4096-byte payload>
 * The whole 82-byte header: 16 fixed + 66 TLV. */
static const uint8_t golden_minimal[] = {
	0x1e, 0xf1, 0xee, 0x1b, 0x52, 0x10, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x42, 0x00,
	0x00, 0x00, 0x15, 0x25, 0x00, 0xf1, 0xff, 0x25, 0x01, 0x00, 0x80, 0x26, 0x02, 0x00,
	0x00, 0x01, 0x00, 0x2c, 0x03, 0x05, 0x31, 0x2e, 0x30, 0x2e, 0x30, 0x25, 0x04, 0x00,
	0x10, 0x24, 0x08, 0x01, 0x30, 0x09, 0x20, 0x01, 0xb8, 0xe2, 0x02, 0x67, 0x94, 0x02,
	0x59, 0xb7, 0x11, 0xd5, 0xd4, 0xdf, 0xf8, 0xb5, 0x15, 0xb2, 0xc7, 0x6e, 0xc4, 0x82,
	0x76, 0x6f, 0xd8, 0x1d, 0x06, 0x2d, 0x74, 0xe9, 0x32, 0xca, 0xb7, 0x18,
};

/* Same tool, with the optional fields the parser has to skip over:
 * -vn 16842752 -vs "1.2.3" --min-version 65536
 * --release-notes "https://mcuhome.org/n", 300-byte payload.
 * 112 bytes: 16 fixed + 96 TLV. */
static const uint8_t golden_full[] = {
	0x1e, 0xf1, 0xee, 0x1b, 0x9c, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x60, 0x00,
	0x00, 0x00, 0x15, 0x25, 0x00, 0xf1, 0xff, 0x25, 0x01, 0x00, 0x80, 0x26, 0x02, 0x00,
	0x00, 0x01, 0x01, 0x2c, 0x03, 0x05, 0x31, 0x2e, 0x32, 0x2e, 0x33, 0x25, 0x04, 0x2c,
	0x01, 0x26, 0x05, 0x00, 0x00, 0x01, 0x00, 0x2c, 0x07, 0x15, 0x68, 0x74, 0x74, 0x70,
	0x73, 0x3a, 0x2f, 0x2f, 0x6d, 0x63, 0x75, 0x68, 0x6f, 0x6d, 0x65, 0x2e, 0x6f, 0x72,
	0x67, 0x2f, 0x6e, 0x24, 0x08, 0x01, 0x30, 0x09, 0x20, 0x50, 0xf2, 0x15, 0x7e, 0x03,
	0x1b, 0xac, 0x6b, 0x7d, 0x08, 0x8d, 0xcf, 0x00, 0xa6, 0x50, 0xc8, 0x6b, 0x47, 0x83,
	0x31, 0x65, 0xe4, 0x28, 0xae, 0x2f, 0xed, 0x5e, 0xae, 0xf6, 0xfa, 0x2b, 0xed, 0x18,
};

/* --- Helpers ------------------------------------------------------------ */

/** Feed a whole buffer in one call. Returns the parser's verdict. */
static int parse_all(const uint8_t *bytes, size_t length, struct mcuhome_ota_header *out,
		     size_t *left)
{
	struct mcuhome_ota_header_parser parser;
	const uint8_t *cursor = bytes;
	size_t remaining = length;
	int rc;

	mcuhome_ota_header_init(&parser);
	rc = mcuhome_ota_header_parse(&parser, &cursor, &remaining, out);
	if (left != NULL) {
		*left = remaining;
	}
	return rc;
}

/** Feed the buffer @p chunk bytes at a time. Returns the last verdict. */
static int parse_chunked(const uint8_t *bytes, size_t length, size_t chunk,
			 struct mcuhome_ota_header *out, size_t *left)
{
	struct mcuhome_ota_header_parser parser;
	size_t offset = 0;
	int rc = -EAGAIN;

	mcuhome_ota_header_init(&parser);
	while (offset < length) {
		size_t take = (length - offset < chunk) ? (length - offset) : chunk;
		const uint8_t *cursor = &bytes[offset];
		size_t remaining = take;

		rc = mcuhome_ota_header_parse(&parser, &cursor, &remaining, out);
		offset += take - remaining;
		if (rc == 0) {
			if (left != NULL) {
				*left = length - offset;
			}
			return 0;
		}
		if (rc != -EAGAIN) {
			return rc;
		}
		offset = (size_t)(cursor - bytes);
	}
	if (left != NULL) {
		*left = 0;
	}
	return rc;
}

/**
 * Build a header around a caller-supplied TLV body.
 *
 * @p tlv_size_override is what goes into the fixed header's size field;
 * pass 0 to use the real body length, which is what a sane writer does.
 */
static size_t build_header(uint8_t *dest, uint32_t magic, uint64_t total, const uint8_t *tlv,
			   size_t tlv_length, uint32_t tlv_size_override)
{
	uint32_t declared = (tlv_size_override != 0U) ? tlv_size_override : (uint32_t)tlv_length;

	for (unsigned int i = 0; i < 4U; i++) {
		dest[i] = (uint8_t)(magic >> (8U * i));
	}
	for (unsigned int i = 0; i < 8U; i++) {
		dest[4U + i] = (uint8_t)(total >> (8U * i));
	}
	for (unsigned int i = 0; i < 4U; i++) {
		dest[12U + i] = (uint8_t)(declared >> (8U * i));
	}
	memcpy(&dest[16], tlv, tlv_length);
	return 16U + tlv_length;
}

/* Every mandatory field, encoded the narrowest way the specification
 * allows: vendor/product as 8-bit unsigneds, version as 32-bit, payload
 * size as 8-bit. A parser that assumed the widths CHIP's tool happens to
 * pick would fail here, and this is legal TLV. */
static const uint8_t tlv_narrow[] = {
	0x15,                               /* anonymous structure */
	0x24, 0x00, 0x11,                   /* [0] vendor  = 0x11, uint8 */
	0x24, 0x01, 0x22,                   /* [1] product = 0x22, uint8 */
	0x26, 0x02, 0x04, 0x03, 0x02, 0x01, /* [2] version = 0x01020304, uint32 */
	0x24, 0x04, 0x40,                   /* [4] payload = 64, uint8 */
	0x18,                               /* end of container */
};

/* --- Golden-input tests ------------------------------------------------- */

ZTEST(ota_image_header, test_golden_minimal)
{
	struct mcuhome_ota_header header;
	size_t left = 0;

	zassert_equal(parse_all(golden_minimal, sizeof(golden_minimal), &header, &left), 0);
	zassert_equal(left, 0, "the golden buffer is exactly the header");
	zassert_equal(header.vendor_id, 0xFFF1);
	zassert_equal(header.product_id, 0x8000);
	zassert_equal(header.software_version, 65536);
	zassert_equal(header.payload_size, 4096);
	zassert_equal(header.total_size, 4178);
	zassert_equal(header.header_tlv_size, 66);
	zassert_str_equal(header.software_version_string, "1.0.0");
	zassert_equal(header.present & MCUHOME_OTA_HDR_REQUIRED, MCUHOME_OTA_HDR_REQUIRED);
}

ZTEST(ota_image_header, test_golden_with_optional_fields)
{
	struct mcuhome_ota_header header;

	zassert_equal(parse_all(golden_full, sizeof(golden_full), &header, NULL), 0);
	zassert_equal(header.vendor_id, 0xFFF1);
	zassert_equal(header.product_id, 0x8000);
	zassert_equal(header.software_version, 0x01010000);
	zassert_equal(header.payload_size, 300);
	zassert_str_equal(header.software_version_string, "1.2.3");
}

ZTEST(ota_image_header, test_payload_follows_the_header)
{
	uint8_t image[sizeof(golden_minimal) + 8];
	struct mcuhome_ota_header header;
	struct mcuhome_ota_header_parser parser;
	const uint8_t *cursor = image;
	size_t remaining = sizeof(image);

	memcpy(image, golden_minimal, sizeof(golden_minimal));
	for (unsigned int i = 0; i < 8U; i++) {
		image[sizeof(golden_minimal) + i] = (uint8_t)(0xA0 + i);
	}

	mcuhome_ota_header_init(&parser);
	zassert_equal(mcuhome_ota_header_parse(&parser, &cursor, &remaining, &header), 0);
	zassert_equal(remaining, 8, "the payload has to survive the header parse");
	zassert_equal(cursor[0], 0xA0, "and the cursor has to point at its first byte");
	zassert_false(mcuhome_ota_header_pending(&parser));
}

ZTEST(ota_image_header, test_split_across_blocks)
{
	/* Every chunk size from one byte up to past the header: the boundary
	 * between header and payload lands somewhere different each time,
	 * including inside the magic and inside a TLV value. */
	for (size_t chunk = 1; chunk <= sizeof(golden_full) + 4U; chunk++) {
		struct mcuhome_ota_header header;
		size_t left = 0;

		zassert_equal(
			parse_chunked(golden_full, sizeof(golden_full), chunk, &header, &left), 0,
			"chunk size %zu", chunk);
		zassert_equal(header.payload_size, 300, "chunk size %zu", chunk);
		zassert_equal(left, 0, "chunk size %zu", chunk);
	}
}

ZTEST(ota_image_header, test_repeated_parse_after_completion_is_a_no_op)
{
	struct mcuhome_ota_header header;
	struct mcuhome_ota_header_parser parser;
	const uint8_t *cursor = golden_minimal;
	size_t remaining = sizeof(golden_minimal);

	mcuhome_ota_header_init(&parser);
	zassert_equal(mcuhome_ota_header_parse(&parser, &cursor, &remaining, &header), 0);

	/* A caller that keeps asking must not have its payload eaten. */
	const uint8_t payload[4] = {1, 2, 3, 4};

	cursor = payload;
	remaining = sizeof(payload);
	zassert_equal(mcuhome_ota_header_parse(&parser, &cursor, &remaining, &header), 0);
	zassert_equal(remaining, sizeof(payload));
	zassert_equal_ptr(cursor, payload);
}

/* --- Hand-built inputs -------------------------------------------------- */

ZTEST(ota_image_header, test_narrow_integer_encodings)
{
	uint8_t image[64];
	struct mcuhome_ota_header header;
	size_t length =
		build_header(image, MCUHOME_OTA_MAGIC, 200, tlv_narrow, sizeof(tlv_narrow), 0);

	zassert_equal(parse_all(image, length, &header, NULL), 0);
	zassert_equal(header.vendor_id, 0x11);
	zassert_equal(header.product_id, 0x22);
	zassert_equal(header.software_version, 0x01020304);
	zassert_equal(header.payload_size, 64);
	zassert_str_equal(header.software_version_string, "",
			  "no version string means an empty one, never a stale pointer");
}

ZTEST(ota_image_header, test_wrong_magic_is_refused)
{
	uint8_t image[64];
	struct mcuhome_ota_header header;
	size_t length = build_header(image, 0xDEADBEEF, 200, tlv_narrow, sizeof(tlv_narrow), 0);

	zassert_equal(parse_all(image, length, &header, NULL), -EILSEQ);
}

ZTEST(ota_image_header, test_truncated_input_asks_for_more)
{
	struct mcuhome_ota_header header;

	/* Short of the fixed header. */
	zassert_equal(parse_all(golden_minimal, 10, &header, NULL), -EAGAIN);
	/* Fixed header complete, TLV incomplete. */
	zassert_equal(parse_all(golden_minimal, 40, &header, NULL), -EAGAIN);
	/* One byte short of the whole thing. */
	zassert_equal(parse_all(golden_minimal, sizeof(golden_minimal) - 1U, &header, NULL),
		      -EAGAIN);
}

ZTEST(ota_image_header, test_zero_length_input_asks_for_more)
{
	struct mcuhome_ota_header_parser parser;
	struct mcuhome_ota_header header;
	const uint8_t *cursor = golden_minimal;
	size_t remaining = 0;

	mcuhome_ota_header_init(&parser);
	zassert_equal(mcuhome_ota_header_parse(&parser, &cursor, &remaining, &header), -EAGAIN);
}

ZTEST(ota_image_header, test_oversized_header_is_refused)
{
	uint8_t image[64];
	struct mcuhome_ota_header header;
	size_t length = build_header(image, MCUHOME_OTA_MAGIC, 200, tlv_narrow, sizeof(tlv_narrow),
				     MCUHOME_OTA_HEADER_MAX + 1U);

	zassert_equal(parse_all(image, length, &header, NULL), -ENOSPC,
		      "a header larger than the static buffer is refused, never truncated");
}

ZTEST(ota_image_header, test_zero_length_header_is_refused)
{
	uint8_t image[64];
	struct mcuhome_ota_header header;
	size_t length =
		build_header(image, MCUHOME_OTA_MAGIC, 200, tlv_narrow, sizeof(tlv_narrow), 0);

	/* A declared TLV size of 0 cannot be expressed through the override
	 * (0 there means "use the real length"), so it is written by hand.
	 * A header with no TLV at all carries none of the mandatory fields
	 * and is a malformed image, not an empty one. */
	image[12] = 0x00;
	image[13] = 0x00;
	image[14] = 0x00;
	image[15] = 0x00;
	zassert_equal(parse_all(image, length, &header, NULL), -EILSEQ);
}

ZTEST(ota_image_header, test_missing_mandatory_field_is_refused)
{
	/* Everything except the payload size. */
	static const uint8_t tlv[] = {
		0x15, 0x24, 0x00, 0x11, 0x24, 0x01, 0x22, 0x26, 0x02, 0x04, 0x03, 0x02, 0x01, 0x18,
	};
	uint8_t image[64];
	struct mcuhome_ota_header header;
	size_t length = build_header(image, MCUHOME_OTA_MAGIC, 200, tlv, sizeof(tlv), 0);

	zassert_equal(parse_all(image, length, &header, NULL), -EILSEQ);
}

ZTEST(ota_image_header, test_unterminated_structure_is_refused)
{
	static const uint8_t tlv[] = {
		0x15, 0x24, 0x00, 0x11, 0x24, 0x01, 0x22, 0x26,
		0x02, 0x04, 0x03, 0x02, 0x01, 0x24, 0x04, 0x40,
		/* no 0x18 */
	};
	uint8_t image[64];
	struct mcuhome_ota_header header;
	size_t length = build_header(image, MCUHOME_OTA_MAGIC, 200, tlv, sizeof(tlv), 0);

	zassert_equal(parse_all(image, length, &header, NULL), -EILSEQ);
}

ZTEST(ota_image_header, test_element_before_the_structure_is_refused)
{
	static const uint8_t tlv[] = {
		0x24, 0x00, 0x11, /* a bare context-tagged uint8 at the top */
		0x15, 0x18,
	};
	uint8_t image[64];
	struct mcuhome_ota_header header;
	size_t length = build_header(image, MCUHOME_OTA_MAGIC, 200, tlv, sizeof(tlv), 0);

	zassert_equal(parse_all(image, length, &header, NULL), -EILSEQ);
}

ZTEST(ota_image_header, test_stray_end_of_container_is_refused)
{
	static const uint8_t tlv[] = {0x18};
	uint8_t image[64];
	struct mcuhome_ota_header header;
	size_t length = build_header(image, MCUHOME_OTA_MAGIC, 200, tlv, sizeof(tlv), 0);

	zassert_equal(parse_all(image, length, &header, NULL), -EILSEQ);
}

ZTEST(ota_image_header, test_string_length_past_the_end_is_refused)
{
	static const uint8_t tlv[] = {
		0x15, 0x2c, 0x03, 0x40, 'x', 'y', /* claims 64 bytes, has 2 */
		0x18,
	};
	uint8_t image[64];
	struct mcuhome_ota_header header;
	size_t length = build_header(image, MCUHOME_OTA_MAGIC, 200, tlv, sizeof(tlv), 0);

	zassert_equal(parse_all(image, length, &header, NULL), -EILSEQ);
}

ZTEST(ota_image_header, test_anonymous_member_is_skipped)
{
	static const uint8_t tlv[] = {
		0x15, 0x04, 0x11, /* an anonymous uint8 inside the structure: legal
				   * TLV that carries no tag, so the parser has to
				   * skip it rather than read it as field 0 */
		0x24, 0x00, 0x11, 0x24, 0x01, 0x22, 0x26, 0x02,
		0x04, 0x03, 0x02, 0x01, 0x24, 0x04, 0x40, 0x18,
	};
	uint8_t image[64];
	struct mcuhome_ota_header header;
	size_t length = build_header(image, MCUHOME_OTA_MAGIC, 200, tlv, sizeof(tlv), 0);

	zassert_equal(parse_all(image, length, &header, NULL), 0,
		      "an anonymous element inside the structure is legal and skipped");
}

ZTEST(ota_image_header, test_nested_container_is_skipped)
{
	static const uint8_t tlv[] = {
		0x15,
		0x24,
		0x00,
		0x11,
		0x24,
		0x01,
		0x22,
		0x26,
		0x02,
		0x04,
		0x03,
		0x02,
		0x01,
		0x24,
		0x04,
		0x40,
		/* A nested list carrying a context tag 0 that must NOT be
		 * read as the vendor ID of the header. */
		0x37,
		0x0A,
		0x24,
		0x00,
		0x99,
		0x18,
		0x18,
	};
	uint8_t image[64];
	struct mcuhome_ota_header header;
	size_t length = build_header(image, MCUHOME_OTA_MAGIC, 200, tlv, sizeof(tlv), 0);

	zassert_equal(parse_all(image, length, &header, NULL), 0);
	zassert_equal(header.vendor_id, 0x11, "a nested element must not shadow a header field");
}

ZTEST(ota_image_header, test_null_arguments_are_refused)
{
	struct mcuhome_ota_header_parser parser;
	struct mcuhome_ota_header header;
	const uint8_t *cursor = golden_minimal;
	size_t remaining = sizeof(golden_minimal);

	mcuhome_ota_header_init(&parser);
	zassert_equal(mcuhome_ota_header_parse(NULL, &cursor, &remaining, &header), -EINVAL);
	zassert_equal(mcuhome_ota_header_parse(&parser, NULL, &remaining, &header), -EINVAL);
	zassert_equal(mcuhome_ota_header_parse(&parser, &cursor, NULL, &header), -EINVAL);
	zassert_equal(mcuhome_ota_header_parse(&parser, &cursor, &remaining, NULL), -EINVAL);
	zassert_false(mcuhome_ota_header_pending(NULL));
}

ZTEST(ota_image_header, test_init_resets_a_used_parser)
{
	struct mcuhome_ota_header_parser parser;
	struct mcuhome_ota_header header;
	const uint8_t *cursor = golden_minimal;
	size_t remaining = sizeof(golden_minimal);

	mcuhome_ota_header_init(&parser);
	zassert_equal(mcuhome_ota_header_parse(&parser, &cursor, &remaining, &header), 0);
	zassert_false(mcuhome_ota_header_pending(&parser));

	/* A second download reuses the same parser. */
	mcuhome_ota_header_init(&parser);
	zassert_true(mcuhome_ota_header_pending(&parser));
	cursor = golden_full;
	remaining = sizeof(golden_full);
	zassert_equal(mcuhome_ota_header_parse(&parser, &cursor, &remaining, &header), 0);
	zassert_equal(header.payload_size, 300);
}

ZTEST_SUITE(ota_image_header, NULL, NULL, NULL, NULL, NULL);
