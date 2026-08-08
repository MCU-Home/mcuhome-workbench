# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
#
# native_sim twister suite for the OTA staging writer
# (components/matter/src/ota_staging.c).
#
# WHAT IT GUARDS. MCUboot's swap-using-offset mode reads the staged
# image's header one erase sector into the secondary slot, not at its
# start. An image written at offset 0 is refused by the bootloader with a
# single log line that the shipping configuration used to compile out —
# so the device downloads, reboots, boots the old image, and nothing
# anywhere says why. That happened. ota_staging.h carries the citations.
#
# The staging writer is its own translation unit precisely so this suite
# can drive it: it needs Zephyr (flash map, flash_img) but not CHIP, and
# native_sim's simulated flash gives it a slot1_partition with 4 KiB
# sectors to write into. The suite reads the slot back through the flash
# map afterwards and asserts where the bytes actually landed — the one
# fact the bench could not see.

cmake_minimum_required(VERSION 3.20.0)

find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(mcuhome_ota_staging_test)

set(MCUHOME_ROOT ${CMAKE_CURRENT_SOURCE_DIR}/../..)
set(MCUHOME_MATTER_SRC_DIR ${MCUHOME_ROOT}/components/matter/src)

target_sources(app PRIVATE
  src/main.c
  ${MCUHOME_MATTER_SRC_DIR}/ota_staging.c
)

target_include_directories(app PRIVATE
  ${MCUHOME_ROOT}/include
  ${MCUHOME_MATTER_SRC_DIR}
)
