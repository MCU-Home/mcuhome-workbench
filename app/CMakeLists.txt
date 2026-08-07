# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
#
# This directory is NOT an application. It holds `src/main.c`, the generic
# MCUHome application main that every generated device shares; `mcuhome
# build` writes the device configuration around it and emits a
# CMakeLists.txt that compiles the two together.
#
# The file exists anyway, and does nothing but refuse, because `west build
# mcuhome/app` was a documented command for as long as this directory held
# the scaffold's hello-world placeholder. Someone will still type it. A
# named refusal at configure time is a better answer than Zephyr's "no
# CMakeLists.txt here", and a far better one than a firmware image that
# builds and does nothing.

cmake_minimum_required(VERSION 3.20.0)

message(FATAL_ERROR
  "mcuhome/app is not a standalone Zephyr application.\n"
  "It holds the generic application main that the MCUHome builder compiles "
  "together with the device configuration it generates from your YAML:\n"
  "  mcuhome build <device> --build-dir <dir>\n"
  "then build <dir>/app, which the builder does for you unless you pass "
  "--generate-only.\n"
  "For a runnable example without the builder, build a sample instead:\n"
  "  west build -p -b nrf7002dk/nrf5340/cpuapp -S matter -S debug-rtt "
  "mcuhome/samples/matter-node")
