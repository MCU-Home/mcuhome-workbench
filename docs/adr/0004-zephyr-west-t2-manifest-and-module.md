# 0004 — Zephyr west T2 manifest repo that is also a Zephyr module

- Status: accepted
- Date: 2026-08-02

## Context

Zephyr projects are organized as west workspaces. Three topologies exist:
T1 (Zephyr itself is the manifest repo), T2 ("star": the application repo
is the manifest repo, importing Zephyr's manifest), T3 ("forest": a bare
manifest-only repo coordinates many repos). The canonical template for
out-of-tree frameworks is `zephyrproject-rtos/example-application`, which
uses T2 and simultaneously declares itself a Zephyr module
(`zephyr/module.yml`), so third parties can consume it either way.

West requires the workspace top directory not to be a git repository.

## Decision

This repository follows the example-application pattern:

- **T2 manifest repo:** `west.yml` pins Zephyr (v4.4.0) and imports only
  the modules we use via `import: name-allowlist:`. Firmware developers run
  `west init -m https://github.com/mcu-home/mcuhome`.
- **Zephyr module:** `zephyr/module.yml` registers `board_root`,
  `dts_root` and `snippet_root`, so advanced users can instead add MCUHome
  to their own manifest as a module.
- All revisions are pinned to tags/SHAs, never `main`. Zephyr and Matter
  SDK pins are bumped as a version-matched pair (see ADR 0006).
- Device-class/connectivity variants (WiFi, Thread FTD/MTD, Thread SED)
  are modeled as Zephyr snippets and Kconfig fragments — configuration,
  not directory structure. The YAML builder composes them.

## Consequences

- Reproducible builds; self-contained framework repo; both consumption
  modes documented in the README.
- The T3 option (separate manifest-only repo) remains open if the project
  ever fragments into many repositories.
- Local checkouts must live one level below a non-git workspace directory;
  documented in README, CONTRIBUTING and AGENTS.md.
