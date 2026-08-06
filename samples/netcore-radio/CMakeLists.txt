# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
#
# MCUHome network-core image for the nRF5340: the upstream
# zephyr/samples/boards/nordic/ieee802154/802154_rpmsg radio server plus
# the MCUHome entropy service (see README.md for why the two live in one
# image).

cmake_minimum_required(VERSION 3.20.0)

find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(mcuhome-netcore-radio)

target_sources(app PRIVATE
    src/main.c
    src/entropy_service.c)

# The wire protocol header is shared verbatim with the application core.
# Pulled in by path rather than by enabling the MCUHome Zephyr module:
# the module's Kconfig tree assumes an application core (Matter, sensors,
# the channel layer) and none of it belongs in a radio image.
target_include_directories(app PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/../../include)
