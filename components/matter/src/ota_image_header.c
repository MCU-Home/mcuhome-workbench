/*
 * SPDX-FileCopyrightText: 2026 The MCUHome Contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * Matter OTA image header parser — see ota_image_header.h for what it is
 * for and why it is MCUHome's own.
 *
 * The header TLV is Matter TLV (Matter 1.4 §A): one anonymous structure
 * whose members carry context tags 0..9. Only five of them matter here
 * (vendor, product, software version and its string, payload size); the
 * rest — minimum/maximum applicable version, release notes URL, digest
 * type and digest — are skipped, which is why this decoder still has to
 * understand every element encoding well enough to know how long each one
 * is. Nested containers cannot occur in a header the specification
 * describes, but they are handled anyway: a decoder that guesses at a
 * length it does not recognize is a decoder that walks off the end of a
 * buffer an attacker filled.
 *
 * A note on what is deliberately NOT here: the digest. CHIP does not
 * verify it on any platform, so verifying it here would create the
 * impression the payload is authenticated when the only thing that
 * authenticates it is MCUboot's signature over the image inside
 * (ADR 0015 decision 6). Parsing a field in order to ignore it is honest;
 * checking a digest that proves nothing about origin is not.
 */

#include <errno.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include "ota_image_header.h"

/* Parser states. */
#define STATE_FIXED 0U
#define STATE_TLV   1U
#define STATE_DONE  2U

/* Matter TLV control byte: tag control in the top three bits, element
 * type in the low five. */
#define TLV_TAG_CONTROL_MASK  0xE0U
#define TLV_ELEMENT_TYPE_MASK 0x1FU

#define TLV_TAG_ANONYMOUS  0x00U
#define TLV_TAG_CONTEXT    0x20U
#define TLV_TAG_COMMON_2   0x40U
#define TLV_TAG_COMMON_4   0x60U
#define TLV_TAG_IMPLICIT_2 0x80U
#define TLV_TAG_IMPLICIT_4 0xA0U
#define TLV_TAG_FULL_6     0xC0U
#define TLV_TAG_FULL_8     0xE0U

#define TLV_TYPE_INT8       0x00U
#define TLV_TYPE_INT64      0x03U
#define TLV_TYPE_UINT8      0x04U
#define TLV_TYPE_UINT64     0x07U
#define TLV_TYPE_BOOL_FALSE 0x08U
#define TLV_TYPE_BOOL_TRUE  0x09U
#define TLV_TYPE_FLOAT32    0x0AU
#define TLV_TYPE_FLOAT64    0x0BU
#define TLV_TYPE_UTF8_1     0x0CU
#define TLV_TYPE_UTF8_8     0x0FU
#define TLV_TYPE_BYTES_1    0x10U
#define TLV_TYPE_BYTES_8    0x13U
#define TLV_TYPE_NULL       0x14U
#define TLV_TYPE_STRUCT     0x15U
#define TLV_TYPE_ARRAY      0x16U
#define TLV_TYPE_LIST       0x17U
#define TLV_TYPE_END        0x18U

/* Context tags of the header structure. */
#define TAG_VENDOR_ID      0U
#define TAG_PRODUCT_ID     1U
#define TAG_VERSION        2U
#define TAG_VERSION_STRING 3U
#define TAG_PAYLOAD_SIZE   4U

/** Little-endian read of 1..8 bytes. Bounds are the caller's job. */
static uint64_t read_le(const uint8_t *data, uint8_t bytes)
{
	uint64_t value = 0;

	for (uint8_t index = 0; index < bytes; index++) {
		value |= (uint64_t)data[index] << (8U * index);
	}
	return value;
}

/** Tag length in bytes for a tag control field, or 0xFF if reserved. */
static uint8_t tag_length(uint8_t tag_control)
{
	switch (tag_control) {
	case TLV_TAG_ANONYMOUS:
		return 0U;
	case TLV_TAG_CONTEXT:
		return 1U;
	case TLV_TAG_COMMON_2:
	case TLV_TAG_IMPLICIT_2:
		return 2U;
	case TLV_TAG_COMMON_4:
	case TLV_TAG_IMPLICIT_4:
		return 4U;
	case TLV_TAG_FULL_6:
		return 6U;
	case TLV_TAG_FULL_8:
		return 8U;
	default:
		return 0xFFU;
	}
}

/**
 * Fixed value length of an element type, the length-prefixed and
 * container types excluded.
 *
 * Integers encode their width in the low two bits of the type: 1, 2, 4
 * and 8 bytes for the four steps of both the signed and unsigned range.
 */
static uint8_t value_length(uint8_t type)
{
	if (type <= TLV_TYPE_INT64 || (type >= TLV_TYPE_UINT8 && type <= TLV_TYPE_UINT64)) {
		return (uint8_t)(1U << (type & 0x03U));
	}
	switch (type) {
	case TLV_TYPE_BOOL_FALSE:
	case TLV_TYPE_BOOL_TRUE:
	case TLV_TYPE_NULL:
		return 0U;
	case TLV_TYPE_FLOAT32:
		return 4U;
	case TLV_TYPE_FLOAT64:
		return 8U;
	default:
		return 0xFFU;
	}
}

/** Copy a UTF-8 string element into the fixed-size destination. */
static void copy_string(char *dest, size_t dest_size, const uint8_t *src, uint64_t length)
{
	size_t taken = (length < dest_size - 1U) ? (size_t)length : dest_size - 1U;

	memcpy(dest, src, taken);
	dest[taken] = '\0';
}

static int decode_tlv(const uint8_t *data, size_t length, struct mcuhome_ota_header *out)
{
	size_t offset = 0;
	unsigned int depth = 0;
	bool seen_root = false;

	while (offset < length) {
		uint8_t control = data[offset++];
		uint8_t type = control & TLV_ELEMENT_TYPE_MASK;
		uint8_t tag_bytes = tag_length(control & TLV_TAG_CONTROL_MASK);
		uint8_t value_bytes;
		uint64_t tag = 0;
		uint64_t element_length = 0;
		bool length_prefixed;

		if (tag_bytes == 0xFFU) {
			return -EILSEQ;
		}

		if (type == TLV_TYPE_END) {
			if (depth == 0U) {
				return -EILSEQ;
			}
			depth--;
			if (depth == 0U) {
				/* End of the header structure. Anything after
				 * it is not ours to interpret. */
				break;
			}
			continue;
		}

		if (offset + tag_bytes > length) {
			return -EILSEQ;
		}
		if (tag_bytes == 1U) {
			tag = data[offset];
		}
		offset += tag_bytes;

		if (type == TLV_TYPE_STRUCT || type == TLV_TYPE_ARRAY || type == TLV_TYPE_LIST) {
			if (depth == 0U) {
				if (seen_root || type != TLV_TYPE_STRUCT) {
					return -EILSEQ;
				}
				seen_root = true;
			}
			depth++;
			continue;
		}
		if (!seen_root) {
			/* A bare element before the header structure. */
			return -EILSEQ;
		}

		length_prefixed = (type >= TLV_TYPE_UTF8_1 && type <= TLV_TYPE_BYTES_8);
		if (length_prefixed) {
			uint8_t size_bytes = (uint8_t)(1U << (type & 0x03U));

			if (offset + size_bytes > length) {
				return -EILSEQ;
			}
			element_length = read_le(&data[offset], size_bytes);
			offset += size_bytes;
			if (element_length > (uint64_t)(length - offset)) {
				return -EILSEQ;
			}
			value_bytes = 0U;
		} else {
			value_bytes = value_length(type);
			if (value_bytes == 0xFFU) {
				return -EILSEQ;
			}
			if (offset + value_bytes > length) {
				return -EILSEQ;
			}
			element_length = value_bytes;
		}

		/* Only members of the header structure itself carry meaning;
		 * anything inside a nested container is skipped whole. */
		if (depth == 1U && (control & TLV_TAG_CONTROL_MASK) == TLV_TAG_CONTEXT) {
			switch (tag) {
			case TAG_VENDOR_ID:
				out->vendor_id = (uint16_t)read_le(&data[offset], value_bytes);
				out->present |= MCUHOME_OTA_HDR_VENDOR_ID;
				break;
			case TAG_PRODUCT_ID:
				out->product_id = (uint16_t)read_le(&data[offset], value_bytes);
				out->present |= MCUHOME_OTA_HDR_PRODUCT_ID;
				break;
			case TAG_VERSION:
				out->software_version =
					(uint32_t)read_le(&data[offset], value_bytes);
				out->present |= MCUHOME_OTA_HDR_SOFTWARE_VERSION;
				break;
			case TAG_PAYLOAD_SIZE:
				out->payload_size = read_le(&data[offset], value_bytes);
				out->present |= MCUHOME_OTA_HDR_PAYLOAD_SIZE;
				break;
			case TAG_VERSION_STRING:
				if (type >= TLV_TYPE_UTF8_1 && type <= TLV_TYPE_UTF8_8) {
					copy_string(out->software_version_string,
						    sizeof(out->software_version_string),
						    &data[offset], element_length);
				}
				break;
			default:
				break;
			}
		}

		offset += (size_t)element_length;
	}

	if (depth != 0U || !seen_root) {
		return -EILSEQ;
	}
	if ((out->present & MCUHOME_OTA_HDR_REQUIRED) != MCUHOME_OTA_HDR_REQUIRED) {
		return -EILSEQ;
	}
	return 0;
}

void mcuhome_ota_header_init(struct mcuhome_ota_header_parser *parser)
{
	if (parser == NULL) {
		return;
	}
	parser->state = STATE_FIXED;
	parser->needed = MCUHOME_OTA_FIXED_HEADER_SIZE;
	parser->fill = 0U;
}

bool mcuhome_ota_header_pending(const struct mcuhome_ota_header_parser *parser)
{
	return parser != NULL && parser->state != STATE_DONE;
}

int mcuhome_ota_header_parse(struct mcuhome_ota_header_parser *parser, const uint8_t **data,
			     size_t *len, struct mcuhome_ota_header *out)
{
	if (parser == NULL || data == NULL || *data == NULL || len == NULL || out == NULL) {
		return -EINVAL;
	}
	if (parser->state == STATE_DONE) {
		return 0;
	}

	while (parser->state != STATE_DONE) {
		size_t take = parser->needed - parser->fill;

		if (take > *len) {
			take = *len;
		}
		memcpy(&parser->buffer[parser->fill], *data, take);
		parser->fill += (uint32_t)take;
		*data += take;
		*len -= take;

		if (parser->fill < parser->needed) {
			return -EAGAIN;
		}

		if (parser->state == STATE_FIXED) {
			uint32_t magic = (uint32_t)read_le(&parser->buffer[0], 4U);
			uint64_t total = read_le(&parser->buffer[4], 8U);
			uint32_t tlv_size = (uint32_t)read_le(&parser->buffer[12], 4U);

			if (magic != MCUHOME_OTA_MAGIC) {
				return -EILSEQ;
			}
			if (tlv_size == 0U) {
				return -EILSEQ;
			}
			if (tlv_size > MCUHOME_OTA_HEADER_MAX) {
				return -ENOSPC;
			}
			memset(out, 0, sizeof(*out));
			out->total_size = total;
			out->header_tlv_size = tlv_size;
			parser->state = STATE_TLV;
			parser->needed = tlv_size;
			parser->fill = 0U;
			continue;
		}

		/* STATE_TLV, fully accumulated. */
		int rc = decode_tlv(parser->buffer, parser->needed, out);

		if (rc != 0) {
			return rc;
		}
		parser->state = STATE_DONE;
		return 0;
	}

	return 0;
}
