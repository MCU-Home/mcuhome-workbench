# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
#
# MCUHome Matter sample node (ADR 0006 / ADR 0014): upstream CHIP
# v1.5.1.0 as Zephyr module on vanilla Zephyr v4.4.0, nRF7002-DK, two
# runtime-registered endpoints (temperature + pressure, BMP180).
#
# The Matter build glue below — the CHIP_ROOT block, the link-order fix,
# the app-common include and the chip_configure_data_model() call — is
# byte-identical to what `mcuhome build` emits, and tests_py/
# test_generate.py asserts that it stays so. This sample IS a generated
# device (ADR 0014 makes its src/mcuhome_config.{c,h} the codegen
# fixture), so a build mechanism found on the bench must never end up in
# only one of the two files. What legitimately differs is the application
# glue: the sample keeps its own src/main.c, which drives this board's
# LEDs, where a generated application uses the framework's generic main.

cmake_minimum_required(VERSION 3.20.0)

# Matter (CHIP) is a Zephyr module the application registers, not one west
# discovers: upstream connectedhomeip has no zephyr/module.yml at its root,
# only the chip-module subdirectory below. It therefore has to be appended
# to ZEPHYR_EXTRA_MODULES before find_package(Zephyr).
#
# CHIP_ROOT is searched for rather than written out, so this file names no
# path belonging to the machine that generated it and keeps working
# wherever the build tree is moved. What it looks for is the west T2
# workspace layout MCUHome pins (west.yml). Three ways, in order:
#
#   1. -DCHIP_ROOT=... or the CHIP_ROOT environment variable — say it.
#   2. next to ZEPHYR_BASE, when the environment has it. `west build` does
#      NOT export ZEPHYR_BASE, so this is the path for environments that
#      set it deliberately (the MCUHome builder is one).
#   3. upwards from this file, for the normal case of a build directory
#      inside the workspace it is built in.
if(NOT DEFINED CHIP_ROOT)
    if(DEFINED ENV{CHIP_ROOT})
        set(CHIP_ROOT $ENV{CHIP_ROOT})
    elseif(DEFINED ENV{ZEPHYR_BASE})
        get_filename_component(CHIP_ROOT
            "$ENV{ZEPHYR_BASE}/../modules/lib/connectedhomeip" REALPATH)
    else()
        set(_mcuhome_search ${CMAKE_CURRENT_SOURCE_DIR})
        while(NOT DEFINED CHIP_ROOT)
            if(EXISTS "${_mcuhome_search}/modules/lib/connectedhomeip")
                set(CHIP_ROOT "${_mcuhome_search}/modules/lib/connectedhomeip")
            endif()
            get_filename_component(_parent ${_mcuhome_search} DIRECTORY)
            if(_parent STREQUAL _mcuhome_search)
                break()
            endif()
            set(_mcuhome_search ${_parent})
        endwhile()
        unset(_mcuhome_search)
        unset(_parent)
    endif()
endif()

if(NOT EXISTS "${CHIP_ROOT}/config/zephyr/chip-module")
    message(FATAL_ERROR
        "The Matter SDK (connectedhomeip) was not found.\n"
        "Looked for a west workspace containing modules/lib/connectedhomeip, "
        "starting at ${CMAKE_CURRENT_SOURCE_DIR}; CHIP_ROOT resolved to "
        "'${CHIP_ROOT}'.\n"
        "The SDK is in the optional west group `matter`, so a workspace that "
        "was never updated with it has no copy:\n"
        "  west config manifest.group-filter +matter && west update\n"
        "If it lives somewhere else entirely, say so: -DCHIP_ROOT=<path>")
endif()

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

# Application glue (plain C — no CHIP headers) plus the generated device
# configuration (ADR 0014), which `mcuhome build` produces from
# docs/design/examples/00-bmp180-two-endpoints.yaml and the builder test
# suite compares byte for byte. The Matter runtime itself lives in the
# framework component, not here.
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
    ZAP_FILE ${ZEPHYR_MCUHOME_MODULE_DIR}/components/matter/zap/mcuhome-root.zap
    ZCL_PATH ${CHIP_ROOT}/src/app/zap-templates/zcl/zcl.json
)
