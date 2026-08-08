/*
 * SPDX-FileCopyrightText: 2026 The MCUHome Contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * Library-internal seam between the fatal-error handler (health.c) and
 * the crash breadcrumb (breadcrumb.c). Not a public header: the record
 * itself is an implementation detail, and what a caller outside lib/health
 * may know about it is the fault count in <mcuhome/health.h>.
 */

#ifndef MCUHOME_LIB_HEALTH_BREADCRUMB_H_
#define MCUHOME_LIB_HEALTH_BREADCRUMB_H_

#include <zephyr/fatal.h>

#ifdef __cplusplus
extern "C" {
#endif

#ifdef CONFIG_MCUHOME_CRASH_BREADCRUMB

/**
 * @brief Record a fatal error into reset-surviving RAM.
 *
 * Called from inside the fatal-error handler, before anything that can
 * block: plain stores only, no logging, no locks, no drivers. @p esf may
 * be NULL (a fatal error raised without an exception frame), in which
 * case PC and LR are recorded as zero.
 */
void mcuhome_health_breadcrumb_record(unsigned int reason, const struct arch_esf *esf);

#else /* !CONFIG_MCUHOME_CRASH_BREADCRUMB */

static inline void mcuhome_health_breadcrumb_record(unsigned int reason,
						    const struct arch_esf *esf)
{
	(void)reason;
	(void)esf;
}

#endif /* CONFIG_MCUHOME_CRASH_BREADCRUMB */

#ifdef __cplusplus
}
#endif

#endif /* MCUHOME_LIB_HEALTH_BREADCRUMB_H_ */
