# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
#
# Zephyr module entry point (see zephyr/module.yml). Included by the Zephyr
# build system for every application that enables the MCUHome module.

if(CONFIG_MCUHOME)
  zephyr_include_directories(include)

  add_subdirectory(components/matter)
  add_subdirectory(components/sensor)
  add_subdirectory(drivers)
  add_subdirectory(lib)

  # Further component, driver and library sources are added here as they
  # are implemented (components/, drivers/, lib/).
endif()

# Outside the CONFIG_MCUHOME guard on purpose: the MCUboot image builds
# with the module present but the runtime off, and this is bootloader-only
# glue (see the symbol's help text in Kconfig).
if(CONFIG_MCUHOME_BOOT_WATCHDOG)
  add_subdirectory(lib/boot_watchdog)
endif()
