/*
 * SPDX-FileCopyrightText: 2026 The MCUHome Contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * CHIP's project configuration is framework-owned (values and rationale
 * live in include/mcuhome/matter/chip_project_config.h). This wrapper
 * exists only because CONFIG_CHIP_PROJECT_CONFIG is resolved relative to
 * the application source directory, so every Matter application — this
 * sample today, generated firmware tomorrow — needs one file at a
 * predictable app-relative path.
 */

#pragma once

#include <mcuhome/matter/chip_project_config.h>
