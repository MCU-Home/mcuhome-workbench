# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
#
# Zephyr module entry point (see zephyr/module.yml). Included by the Zephyr
# build system for every application that enables the MCUHome module.

if(CONFIG_MCUHOME)
  zephyr_include_directories(include)

  # Component, driver and library sources are added here as they are
  # implemented (components/, drivers/, lib/).
endif()
