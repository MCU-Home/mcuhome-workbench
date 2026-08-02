# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
#
# Minimal placeholder application. It exists so the workspace has one
# buildable target from day one; the MCUHome builder will later generate
# real applications from YAML device configurations.

cmake_minimum_required(VERSION 3.20.0)

find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(mcuhome_app LANGUAGES C)

target_sources(app PRIVATE src/main.c)
