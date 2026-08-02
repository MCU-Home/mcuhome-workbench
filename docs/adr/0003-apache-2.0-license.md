# 0003 — Apache-2.0 as the single project license

- Status: accepted
- Date: 2026-08-02

## Context

MCUHome builds strictly on Zephyr RTOS (Apache-2.0) and Matter /
connectedhomeip (Apache-2.0). ESPHome uses a split license — GPLv3 for its
C++ runtime, MIT for the Python codebase — which causes recurring confusion
for external components and blocks two-way code exchange with permissive
ecosystems. Apache-2.0 code may legally be incorporated into GPLv3 works,
but GPLv3 code can never flow back into the Apache-2.0 Zephyr/Matter
ecosystem, and GPLv3's anti-tivoization clause deters hardware-vendor
adoption. Apache-2.0 additionally carries an explicit patent grant that MIT
lacks — relevant in the Matter/Thread patent environment.

Notably, ESPHome's own 2026 greenfield code (Device Builder) chose
Apache-2.0.

## Decision

Apache-2.0 for everything: firmware, builder, dashboard. No split license.
Implementation follows the REUSE 3.3 specification: `LICENSES/` directory,
SPDX headers in every file, `REUSE.toml` for files that cannot carry
headers, `reuse lint` in pre-commit.

Corollary: **no code may ever be ported from GPL projects — explicitly
including ESPHome's C++ runtime.** ESPHome is inspiration, never a source.

## Consequences

- Full license alignment with Zephyr, Matter and Home Assistant; code can
  be upstreamed and vendored in both directions.
- Commercial/vendor adoption stays open.
- No copyleft protection: proprietary forks are possible. We accept this
  trade-off, as Zephyr and Matter themselves do.
- Exception: `CODE_OF_CONDUCT.md` is CC-BY-SA-4.0 (Contributor Covenant's
  own license), tracked in `REUSE.toml` and `LICENSES/`.
