# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
#
# Out-of-tree device drivers (drivers/README.md). Each subdirectory's
# add_subdirectory() call below is guarded by the Kconfig symbol that
# gates it, so the subdirectory — and therefore its zephyr_library() call
# — is only ever entered when that symbol is enabled.

if(CONFIG_MCUHOME_ENTROPY_IPC)
  add_subdirectory(entropy)
elseif(CONFIG_DT_HAS_MCUHOME_ENTROPY_IPC_ENABLED)
  # The node is in the devicetree but the driver did not come up, so the
  # application has an entropy source on paper and none in the image.
  # Left alone this surfaces as a link error against a nonexistent
  # device, or — worse, if something else provides one — as a silent
  # switch to a different entropy source. Name the actual cause instead.
  message(FATAL_ERROR
    "A 'mcuhome,entropy-ipc' devicetree node is enabled but "
    "CONFIG_MCUHOME_ENTROPY_IPC is not set. It needs CONFIG_ENTROPY_GENERATOR, "
    "CONFIG_IPC_SERVICE and CONFIG_MBEDTLS_PSA_CRYPTO_C; check which is missing.")
endif()
