# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
#
# Only entered when CONFIG_MCUHOME_BOOT_WATCHDOG is set (top-level
# CMakeLists.txt), which the generated sysbuild/mcuboot.conf does for the
# bootloader image and nothing does for the application.
#
# target_sources(app ...) instead of zephyr_library(): boot_watchdog.c
# carries a STRONG definition of mcuboot_watchdog_setup(), whose weak
# counterpart sits in an object file of the MCUboot application target.
# An archive member is only pulled in to resolve an *undefined* symbol —
# a weak definition already satisfies the reference, so a library version
# of this file would be silently ignored. As an object of the same target
# it wins the way strong symbols always win.

target_sources(app PRIVATE boot_watchdog.c)
