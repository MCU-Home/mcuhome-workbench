/*
 * SPDX-FileCopyrightText: 2026 The MCUHome Contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * Framework-owned CHIP project configuration.
 *
 * CHIP's generic configuration headers include this file (via the
 * chip_project_config_include GN argument, fed from
 * CONFIG_CHIP_PROJECT_CONFIG) to override SDK defaults. Because
 * CONFIG_CHIP_PROJECT_CONFIG is resolved relative to the *application*
 * source directory, an app keeps a one-line wrapper that includes this
 * header; the values themselves belong to the framework, not to any
 * device.
 *
 * VERIFIED (2026-08-05, CHIP v1.5.1.0 + Zephyr v4.4.0): the Zephyr
 * chip-module forwards Zephyr's compile flags into the inner GN build with
 * zephyr_get_compile_flags(), and those flags carry
 * `-imacros <build>/zephyr/include/generated/zephyr/autoconf.h` — so
 * CONFIG_* symbols ARE visible inside CHIP's own compilation, not just in
 * the Zephyr app. `mcuhome/include` is forwarded the same way (it is on
 * zephyr_interface via zephyr_include_directories()), which is what makes
 * this header reachable at all. The #error below turns any regression of
 * either mechanism into a loud build failure instead of a silently wrong
 * SDK configuration.
 */

#ifndef MCUHOME_MATTER_CHIP_PROJECT_CONFIG_H_
#define MCUHOME_MATTER_CHIP_PROJECT_CONFIG_H_

/* Belt and braces: -imacros already defines these, but an explicit include
 * keeps the header self-contained if it is ever pulled into a translation
 * unit that is compiled without Zephyr's imacros (e.g. a host-side test). */
#if defined(__has_include)
#if __has_include(<zephyr/autoconf.h>)
#include <zephyr/autoconf.h>
#endif
#endif

#if !defined(CONFIG_MCUHOME_MATTER_MAX_DYNAMIC_ENDPOINTS)
#error "MCUHome Kconfig symbols are not visible in the CHIP compile. " \
	"Expected -imacros .../generated/zephyr/autoconf.h in the flags the " \
	"Zephyr chip-module forwards to the CHIP GN build. Do not paper over " \
	"this with literals — an out-of-sync CHIP_DEVICE_CONFIG_DYNAMIC_ENDPOINT_COUNT " \
	"drops Descriptor cluster registration silently (ADR 0014)."
#endif

/*
 * Slots for runtime-registered endpoints. Derived from the same Kconfig
 * symbol that sizes the framework's own translation pools, on purpose: if
 * CHIP's registry is smaller than the framework's pools, endpoint
 * registration still succeeds but the Descriptor cluster instance is
 * dropped upstream without an error (ADR 0014, decision B). The SDK
 * default is 0, which makes every emberAfSetDynamicEndpoint() fail with
 * CHIP_ERROR_NO_MEMORY.
 */
#define CHIP_DEVICE_CONFIG_DYNAMIC_ENDPOINT_COUNT CONFIG_MCUHOME_MATTER_MAX_DYNAMIC_ENDPOINTS

/*
 * PASE timing. Software SPAKE2+ on a plain Cortex-M33 takes ~3.2 s per step
 * (measured on nRF5340); with the Thread default MRP intervals (SAI 2 s)
 * the commissioner's retransmission budget (~3.3 s) expires first and every
 * commissioning attempt dies as "address unreachable". Advertise larger
 * retry intervals so controllers wait for us. The p256-m driver and the
 * mbedTLS bignum assembly (both enabled by the `matter` snippet) cut the
 * compute time down; these intervals are the safety margin on top.
 *
 * CHIP wants System::Clock::Milliseconds32 literals here, hence the
 * two-level paste that expands the Kconfig macro before gluing on the
 * `_ms32` user-defined literal suffix.
 */
#define MCUHOME_MATTER_MS32_(x) x##_ms32
#define MCUHOME_MATTER_MS32(x) MCUHOME_MATTER_MS32_(x)

#define CHIP_CONFIG_MRP_LOCAL_ACTIVE_RETRY_INTERVAL                                                \
	MCUHOME_MATTER_MS32(CONFIG_MCUHOME_MATTER_MRP_ACTIVE_RETRY_MS)
#define CHIP_CONFIG_MRP_LOCAL_IDLE_RETRY_INTERVAL                                                  \
	MCUHOME_MATTER_MS32(CONFIG_MCUHOME_MATTER_MRP_IDLE_RETRY_MS)

#endif /* MCUHOME_MATTER_CHIP_PROJECT_CONFIG_H_ */
