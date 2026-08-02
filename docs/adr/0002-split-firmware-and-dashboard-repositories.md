# 0002 — Split firmware and dashboard into separate repositories

- Status: accepted
- Date: 2026-08-02

## Context

MCUHome consists of a firmware framework (Zephyr C runtime + Python YAML
builder) and a web interface. ESPHome's history provides three relevant
lessons: (1) they originally split the Python codegen (esphomeyaml) from the
C++ runtime (esphome-core) and merged them in 2019 because the two must
version in lockstep; (2) their dashboard lived inside the core repo for
years and could only be replaced in 2026 by a standalone, from-scratch
product (Device Builder); (3) Home Assistant add-on packaging has its own format
and release cadence and lives in a thin separate repo.

The stacks are also maximally heterogeneous: Zephyr/west/CMake/C vs. web
tooling — separate CI, separate release cadences.

## Decision

Two product repositories from day one:

- **`mcu-home/mcuhome`** — firmware framework: Zephyr runtime, components,
  and the Python YAML builder in ONE repo (codegen and runtime in lockstep).
- **`mcu-home/dashboard`** — the web interface as a standalone product with
  its own release cycle.

A thin `mcu-home/home-assistant-addon` packaging repo follows later, as
does an org-level `.github` repo for shared community defaults.

## Consequences

- No painful dashboard extraction later; independent release cadences.
- Cross-repo contracts (YAML schema, device metadata) must be owned by the
  firmware repo and consumed by the dashboard as a versioned artifact —
  this needs an explicit ADR when the builder API is designed.
- Contributors touching both sides open two PRs; mitigated by clear
  contract ownership.
