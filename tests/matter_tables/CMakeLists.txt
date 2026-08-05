# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
#
# native_sim twister suite for the CHIP-free tables-contract validator
# (components/matter/src/table_validate.c, ADR 0014). Pulls the validator
# source directly from the framework component instead of enabling the
# mcuhome Zephyr module: CONFIG_MCUHOME_MATTER depends on CHIP, and CHIP
# cannot build on native_sim, so this test app stays entirely outside that
# dependency chain while still compiling and exercising the exact file the
# firmware links.

cmake_minimum_required(VERSION 3.20.0)

find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(mcuhome_matter_tables_test)

set(MCUHOME_ROOT ${CMAKE_CURRENT_SOURCE_DIR}/../..)
set(MCUHOME_MATTER_SRC_DIR ${MCUHOME_ROOT}/components/matter/src)

target_sources(app PRIVATE
  src/main.c
  ${MCUHOME_MATTER_SRC_DIR}/table_validate.c
)

target_include_directories(app PRIVATE
  ${MCUHOME_ROOT}/include
  ${MCUHOME_MATTER_SRC_DIR}
)
