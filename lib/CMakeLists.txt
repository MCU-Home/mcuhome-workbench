# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
#
# Portable runtime libraries (lib/README.md). Each subdirectory's
# add_subdirectory() call below is guarded by the Kconfig symbol that
# gates it, so the subdirectory — and therefore its zephyr_library() call
# — is only ever entered when that symbol is enabled.

if(CONFIG_MCUHOME_RTT_REINIT)
  add_subdirectory(debug)
endif()

if(CONFIG_MCUHOME_HEALTH)
  add_subdirectory(health)
endif()
