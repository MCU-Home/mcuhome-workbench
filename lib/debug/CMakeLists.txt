# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
#
# Only entered when CONFIG_MCUHOME_RTT_REINIT is set (see
# lib/CMakeLists.txt), so this zephyr_library() is guaranteed at least one
# source below — Zephyr treats a sourceless library as a CMake error.

zephyr_library()
zephyr_library_sources(rtt_reinit.c)
