# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
#
# Only entered when CONFIG_MCUHOME_HEALTH is set (see lib/CMakeLists.txt).
# health.c is unconditional inside that: it carries the fatal-error handler
# and the log module image_confirm.c declares.

zephyr_library()
zephyr_library_sources(health.c)
zephyr_library_sources_ifdef(CONFIG_MCUHOME_IMAGE_CONFIRM image_confirm.c)
