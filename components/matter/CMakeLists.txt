# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
#
# MCUHome Matter framework component (docs/design/component-model.md §2,
# "core" kind): the Matter runtime the builder drives from generated
# device tables (ADR 0014) — bring-up sequence, tables-to-ember
# translation, external attribute callbacks, DNS-SD retry service.

# Loud failure for the classic mistake: an app that enables CHIP but ends
# up with CONFIG_MCUHOME_MATTER=n. That happens when the `matter` snippet
# is missing, because MCUHOME_MATTER depends on PICOLIBC (see Kconfig) and
# an unsatisfied dependency silently drops the assignment. Without this
# check the build fails much later, at link time, on missing
# mcuhome_matter_start().
if(CONFIG_CHIP AND NOT CONFIG_MCUHOME_MATTER)
  message(FATAL_ERROR
    "CONFIG_CHIP is enabled but CONFIG_MCUHOME_MATTER is not.\n"
    "Its dependencies come from the framework snippet — build with -S matter:\n"
    "  west build -b <board> -S matter -S debug-rtt <app>")
endif()

if(CONFIG_MCUHOME_MATTER)
  # Same guard as the BUILD_ASSERTs in src/matter_init.cpp, hoisted to
  # configure time: those thresholds are only reached after the whole CHIP
  # library has been built, which is ten minutes of waiting for a mistake
  # that is knowable now. The BUILD_ASSERTs stay as the backstop — they
  # also catch a build that reaches the compiler by some other route.
  # Keep the numbers in sync with src/matter_init.cpp and snippets/matter.
  if(CONFIG_MAIN_STACK_SIZE LESS 8192 OR CONFIG_MBEDTLS_HEAP_SIZE LESS 15360)
    message(FATAL_ERROR
      "MCUHome Matter: memory sizing too small for the Matter stack "
      "(CONFIG_MAIN_STACK_SIZE=${CONFIG_MAIN_STACK_SIZE}, need >= 8192; "
      "CONFIG_MBEDTLS_HEAP_SIZE=${CONFIG_MBEDTLS_HEAP_SIZE}, need >= 15360).\n"
      "These values come from the framework snippet — build with -S matter:\n"
      "  west build -b <board> -S matter -S debug-rtt <app>")
  endif()

  zephyr_library()
  zephyr_library_sources(
    src/matter_init.cpp
    src/endpoint_registry.cpp
    src/table_validate.c
    src/dnssd_retry.cpp
  )

  # Compile in the same CHIP data-model context as the application.
  #
  # A plain zephyr_library() does NOT see CHIP: the `chip` target is
  # registered as a Zephyr interface library, and Zephyr links those to
  # the `app` target only. On top of that, three include paths exist per
  # application and not globally: the ZAP-generated zap-generated/ tree
  # (endpoint_config.h -> FIXED_ENDPOINT_COUNT), the .matter codegen tree,
  # and zzz_generated/app-common. All of them are created by
  # chip_configure_data_model(app ...) in the application's CMakeLists,
  # which runs AFTER this module directory — hence generator expressions,
  # which are evaluated at generate time and therefore see the final
  # state of both targets.
  zephyr_library_include_directories(
    $<TARGET_PROPERTY:app,INCLUDE_DIRECTORIES>
    $<TARGET_PROPERTY:chip,INTERFACE_INCLUDE_DIRECTORIES>
  )
  zephyr_library_compile_definitions(
    $<TARGET_PROPERTY:chip,INTERFACE_COMPILE_DEFINITIONS>
  )
  # Carries -DCHIP_ADDRESS_RESOLVE_IMPL_INCLUDE_HEADER=..., which
  # chip_data_model.cmake sets on the app target and CHIP's headers
  # require.
  zephyr_library_compile_options(
    $<TARGET_PROPERTY:app,COMPILE_OPTIONS>
  )

  # Build ordering: every generated header above must exist before this
  # library compiles, and only `app` inherits those dependencies — through
  # chip_configure_data_model() for the two codegen targets and through
  # linking `chip` for chip-gn, which produces CHIP's own *BuildConfig.h
  # headers in the GN build tree.
  add_dependencies(${ZEPHYR_CURRENT_LIBRARY} app-zapgen app-codegen chip-gn)
endif()
