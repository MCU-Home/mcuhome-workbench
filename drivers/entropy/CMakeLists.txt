# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
#
# Only entered when CONFIG_MCUHOME_ENTROPY_IPC is set (see
# drivers/CMakeLists.txt), so this zephyr_library() is guaranteed at least
# one source below — Zephyr treats a sourceless library as a CMake error.

zephyr_library()
zephyr_library_sources(
  entropy_ipc_core.c
  entropy_mcuhome_ipc.c
)

# CTR-DRBG comes from the Mbed TLS module. The include directories reach
# this library through zephyr_interface anyway, but the explicit link
# also carries the MBEDTLS_CONFIG_FILE / TF_PSA_CRYPTO_CONFIG_FILE
# definitions the headers need to agree with the library build.
zephyr_library_link_libraries(mbedTLS)
