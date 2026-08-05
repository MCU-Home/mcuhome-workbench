# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
#
# Out-of-tree device drivers (drivers/README.md). Each subdirectory's
# add_subdirectory() call below is guarded by the Kconfig symbol that
# gates it, so the subdirectory — and therefore its zephyr_library() call
# — is only ever entered when that symbol is enabled.

if(CONFIG_MCUHOME_PROTO_ENTROPY)
  add_subdirectory(entropy)
endif()
