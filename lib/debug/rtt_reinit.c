/*
 * SPDX-FileCopyrightText: 2026 The MCUHome Contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * Re-initializes the SEGGER RTT control block before the log core starts.
 * See lib/debug/Kconfig (MCUHOME_RTT_REINIT).
 */

#include <zephyr/init.h>

#include <SEGGER_RTT.h>

/* Log-only RTT builds never re-initialize the RTT control block: the log
 * backend writes through the NoLock API, which skips SEGGER's lazy init.
 * After a reset the block keeps stale read/write offsets from the previous
 * session — the buffer can appear permanently full, wedging the log thread
 * and silencing all output. Initialize explicitly before the log core.
 *
 * upstream: Zephyr candidate Z7 (see UPSTREAM-BUGS.md)
 */
static int mcuhome_rtt_init(void)
{
	SEGGER_RTT_Init();
	return 0;
}
SYS_INIT(mcuhome_rtt_init, PRE_KERNEL_1, 0);
