# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
#
# MCUHome Matter sample node (ADR 0006 / ADR 0014): upstream CHIP
# v1.5.1.0 as Zephyr module on vanilla Zephyr v4.4.0, nRF7002-DK, two
# runtime-registered endpoints (temperature + pressure, BMP180).

cmake_minimum_required(VERSION 3.20.0)

get_filename_component(CHIP_ROOT
    ${CMAKE_CURRENT_SOURCE_DIR}/../../../modules/lib/connectedhomeip REALPATH)

list(APPEND ZEPHYR_EXTRA_MODULES ${CHIP_ROOT}/config/zephyr/chip-module)

find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(mcuhome-matter-node)

include(${CHIP_ROOT}/src/app/chip_data_model.cmake)

# Link-order fix: the Matter libraries form their own --start/end-group,
# placed after libkernel.a in the final link line, so k_msgq_* references
# from libCHIP.a stay unresolved. Appending the kernel as a *file path*
# (not the target — CMake would dedupe that against the earlier
# occurrence) puts it after the group.
target_link_libraries(chip INTERFACE $<TARGET_FILE:kernel>)

target_include_directories(app PRIVATE
    ${CHIP_ROOT}/zzz_generated/app-common)

# Application glue (plain C — no CHIP headers) plus the hand-written
# "as-if-generated" device tables (ADR 0014). The Matter runtime itself
# lives in the framework component, not here.
target_sources(app PRIVATE
    src/main.c
    src/mcuhome_config.c)

# Framework-owned data model: endpoint 0 (root node) only — every device
# endpoint is registered at runtime (ADR 0014, native composed node).
# ZCL_PATH must be passed explicitly: the zcl.json path stored inside the
# .zap is relative to CHIP's own example directories, so it cannot resolve
# for a .zap that lives outside the CHIP tree (documented escape hatch in
# chip_data_model.cmake). The .matter IDL is inferred from the .zap name.
chip_configure_data_model(app
    ZAP_FILE ${CMAKE_CURRENT_SOURCE_DIR}/../../components/matter/zap/mcuhome-root.zap
    ZCL_PATH ${CHIP_ROOT}/src/app/zap-templates/zcl/zcl.json
)
