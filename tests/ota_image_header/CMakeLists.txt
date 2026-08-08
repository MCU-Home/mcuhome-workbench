# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
#
# native_sim twister suite for the Matter OTA image header parser
# (components/matter/src/ota_image_header.c, ADR 0015 decision 5). Same
# trick as tests/matter_tables and tests/channel: pull the source in
# directly instead of enabling the mcuhome Zephyr module, because
# CONFIG_MCUHOME_MATTER depends on CHIP and CHIP cannot build on
# native_sim — while the parser itself has no Zephyr and no CHIP
# dependency at all, which is exactly why it is its own translation unit.
#
# The image processor around it (ota_image_processor.cpp) is deliberately
# NOT built here: it is CHIP interface, flash-map and stream_flash glue,
# and exercising it host-side would test Zephyr's flash simulator rather
# than MCUHome code.

cmake_minimum_required(VERSION 3.20.0)

find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(mcuhome_ota_image_header_test)

set(MCUHOME_ROOT ${CMAKE_CURRENT_SOURCE_DIR}/../..)
set(MCUHOME_MATTER_SRC_DIR ${MCUHOME_ROOT}/components/matter/src)

target_sources(app PRIVATE
  src/main.c
  ${MCUHOME_MATTER_SRC_DIR}/ota_image_header.c
)

target_include_directories(app PRIVATE
  ${MCUHOME_ROOT}/include
  ${MCUHOME_MATTER_SRC_DIR}
)
