# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
#
# MCUHome Matter framework component (docs/design/component-model.md §2,
# "core" kind): platform plumbing wiring upstream CHIP into the MCUHome
# runtime. C sources land in Block A4; this skeleton only reserves the
# guarded slot so the module tree configures cleanly with
# CONFIG_MCUHOME_MATTER=y before any implementation exists.

if(CONFIG_MCUHOME_MATTER)
  # zephyr_library() and its sources are added in Block A4. Left empty on
  # purpose for now: a zephyr_library() declared with zero sources is a
  # CMake error, so nothing is declared here yet.
endif()
