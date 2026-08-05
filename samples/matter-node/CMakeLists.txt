# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
#
# MCUHome Matter sample node (ADR 0006 / builder-pipeline §4 evaluation):
# upstream CHIP v1.5.1.0 as Zephyr module on vanilla Zephyr v4.4.0,
# nRF7002-DK, one dynamically registered temperature endpoint.

cmake_minimum_required(VERSION 3.20.0)

get_filename_component(CHIP_ROOT
    ${CMAKE_CURRENT_SOURCE_DIR}/../../../modules/lib/connectedhomeip REALPATH)

list(APPEND ZEPHYR_EXTRA_MODULES ${CHIP_ROOT}/config/zephyr/chip-module)

find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(mcuhome-matter-proto)

include(${CHIP_ROOT}/src/app/chip_data_model.cmake)

# Link-order fix: the Matter libraries form their own --start/end-group,
# placed after libkernel.a in the final link line, so k_msgq_* references
# from libCHIP.a stay unresolved. Appending the kernel as a *file path*
# (not the target — CMake would dedupe that against the earlier
# occurrence) puts it after the group.
target_link_libraries(chip INTERFACE $<TARGET_FILE:kernel>)

target_include_directories(app PRIVATE
    ${CHIP_ROOT}/zzz_generated/app-common
    ${CHIP_ROOT}/examples/bridge-app/bridge-common/include)

target_sources(app PRIVATE
    src/main.cpp
    ${CHIP_ROOT}/examples/bridge-app/bridge-common/src/bridged-actions-stub.cpp)

chip_configure_data_model(app
    ZAP_FILE ${CHIP_ROOT}/examples/bridge-app/bridge-common/bridge-app.zap
)

target_sources_ifdef(CONFIG_MCUHOME_PROTO_ENTROPY app PRIVATE src/proto_entropy.c)
