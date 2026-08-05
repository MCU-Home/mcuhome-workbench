# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
#
# native_sim twister suite for the channel layer's arithmetic
# (components/sensor/src/sensor_convert.c). Same trick as
# tests/matter_tables: pull the source in directly instead of enabling the
# mcuhome Zephyr module. CONFIG_MCUHOME_SENSOR depends on
# CONFIG_MCUHOME_MATTER and therefore on CHIP, which cannot build on
# native_sim — while sensor_convert.c itself has no Zephyr and no CHIP
# dependency at all, which is exactly why the arithmetic was factored out
# of the poller in the first place.
#
# The poller (sensor_poll.c) is deliberately NOT built here: it is device,
# workqueue and Matter-reporting glue, and testing it host-side would mean
# testing Zephyr's sensor emulator plumbing rather than MCUHome code.

cmake_minimum_required(VERSION 3.20.0)

find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(mcuhome_channel_test)

set(MCUHOME_ROOT ${CMAKE_CURRENT_SOURCE_DIR}/../..)
set(MCUHOME_SENSOR_SRC_DIR ${MCUHOME_ROOT}/components/sensor/src)

target_sources(app PRIVATE
  src/main.c
  ${MCUHOME_SENSOR_SRC_DIR}/sensor_convert.c
)

target_include_directories(app PRIVATE
  ${MCUHOME_ROOT}/include
  ${MCUHOME_SENSOR_SRC_DIR}
)
