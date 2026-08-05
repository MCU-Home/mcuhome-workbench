# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
#
# MCUHome generic sensor channel adapter (docs/design/component-model.md
# §2, "peripheral" kind): the shared runtime every sensor peripheral
# component uses instead of writing C of its own.
#
# Unlike components/matter this library is CHIP-free — it talks to the
# Matter runtime through <mcuhome/matter.h> only — so it needs none of
# that component's generator-expression include plumbing.

if(CONFIG_MCUHOME_SENSOR)
  zephyr_library()
  zephyr_library_sources(
    src/sensor_poll.c
    src/sensor_convert.c
  )
endif()
