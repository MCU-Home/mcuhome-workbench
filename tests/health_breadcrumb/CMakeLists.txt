# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
#
# native_sim twister suite for the crash breadcrumb's logic
# (lib/health/breadcrumb_core.h).
#
# WHAT IT GUARDS. The breadcrumb is written from inside a fault handler
# and read one reset later, out of RAM that nothing initializes. Neither
# half can be observed on the bench without deliberately crashing a board
# and hoping the reader is attached — and the interesting cases are the
# ones that are hardest to stage: junk that happens to look like a valid
# record, a record clobbered by the bootloader, a fault count that has to
# keep counting across the reset.
#
# breadcrumb_core.h is a header of pure functions over a caller-provided
# struct for exactly this reason: no Zephyr, no SCB registers, no linker
# sections. The suite instantiates the record on its own stack and drives
# every branch of it.
#
# breadcrumb.c is built here too, against its own real `__noinit` record,
# so that the file that ships is compiled and its record/report/count path
# is executed rather than only described. What that cannot cover is the
# one thing only hardware can answer — whether the RAM really survives the
# reset, and whether MCUboot left it alone — which is why the record
# carries an integrity word instead of trusting that it does.

cmake_minimum_required(VERSION 3.20.0)

find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(mcuhome_health_breadcrumb_test)

set(MCUHOME_ROOT ${CMAKE_CURRENT_SOURCE_DIR}/../..)

target_sources(app PRIVATE
  src/main.c
  ${MCUHOME_ROOT}/lib/health/breadcrumb.c
)

target_include_directories(app PRIVATE
  ${MCUHOME_ROOT}/include
  ${MCUHOME_ROOT}/lib/health
)
