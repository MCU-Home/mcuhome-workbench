# 0007 — Containerized toolchain, minimal host requirements

- Status: accepted
- Date: 2026-08-02

## Context

Zephyr development normally requires a locally installed Zephyr SDK
(cross-compilers for every target architecture), west, Python tooling and
per-vendor flash utilities — a heavy, error-prone host setup. MCUHome must
additionally run as a Home Assistant add-on, where the entire build system
has to live inside a single Docker container anyway. The Zephyr project
publishes official CI/build container images
(`ghcr.io/zephyrproject-rtos/ci`) that bundle the SDK.

## Decision

Product-owner requirement: keep host prerequisites minimal.

- **On the developer machine:** only ubiquitous tools — git, docker, and
  optionally make/cmake for convenience wrappers. No Zephyr SDK, no
  cross-compilers, no vendor flash tools on the host.
- **In containers:** toolchains, Zephyr SDK, west, codegen, and flashing
  utilities. The MCUHome builder image is the single build environment,
  used identically by developers, CI, and the Home Assistant add-on.
- Container images are versioned in lockstep with the Zephyr pin in
  `west.yml` (one image per MCUHome release).

## Consequences

- Reproducible builds everywhere; "works in the container" equals "works
  in CI" equals "works in the add-on".
- Flashing from inside containers needs USB passthrough, which is fiddly
  (especially under Home Assistant OS) — device flashing will therefore
  prefer browser-based flashing (WebSerial/WebUSB, as popularized by ESP
  Web Tools) and OTA updates over container-side USB access. Details are
  a design-phase topic.
- The exact image layout (base image, layering, registry) is decided when
  the builder is implemented; this ADR fixes the principle.
