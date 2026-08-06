# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
#
# native_sim twister suite for the entropy driver's DRBG and state
# machine (drivers/entropy/entropy_ipc_core.c) plus the shared wire
# framing. Same trick as tests/channel and tests/matter_tables: pull the
# source in directly instead of enabling the mcuhome Zephyr module.
#
# CONFIG_MCUHOME_ENTROPY_IPC depends on IPC_SERVICE, which needs a real
# multi-core backend and does not exist on native_sim. The core layer
# itself depends on nothing but the kernel and Mbed TLS — that split is
# exactly why the transport lives in its own translation unit behind
# `struct mcuhome_entropy_seed_source`, which the fake provider in
# src/main.c substitutes.

cmake_minimum_required(VERSION 3.20.0)

find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(mcuhome_entropy_ipc_test)

set(MCUHOME_ROOT ${CMAKE_CURRENT_SOURCE_DIR}/../..)
set(MCUHOME_ENTROPY_SRC_DIR ${MCUHOME_ROOT}/drivers/entropy)

target_sources(app PRIVATE
  src/main.c
  ${MCUHOME_ENTROPY_SRC_DIR}/entropy_ipc_core.c
)

target_include_directories(app PRIVATE
  ${MCUHOME_ROOT}/include
  ${MCUHOME_ENTROPY_SRC_DIR}
)

target_link_libraries(app PRIVATE mbedTLS)
