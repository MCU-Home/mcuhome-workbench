# 0006 — Matter SDK source: upstream CHIP vs. Nordic fork

- Status: deferred
- Date: 2026-08-02

## Context

Matter support requires the connectedhomeip (CHIP) SDK as a west project.
Two viable sources exist:

- **Upstream `project-chip/connectedhomeip`:** vendor-neutral, but its
  Zephyr support (`config/zephyr/chip-module`) is primarily exercised
  through downstream SDKs.
- **Nordic's `nrfconnect/sdk-connectedhomeip` fork:** battle-tested against
  Zephyr-based products, version-matched to NCS releases, but couples us to
  Nordic's release cadence and patch set.

Either way, CHIP vendors its dependencies as git submodules (nlio,
nlassert, pigweed, jsoncpp, …), so the west manifest entry must list them
explicitly via the `submodules:` key, pinned by SHA, in an optional west
group (`matter`) so contributors not working on Matter can skip the very
large clone. CHIP's GN/Pigweed build system also adds toolchain
requirements to dev environments and CI.

## Decision

Deferred until the Matter integration milestone. The decision needs a
prototype of both variants against our pinned Zephyr version (v4.4.0),
evaluating: patch delta vs. upstream, OpenThread/SED (ICD) support
maturity, build system friction, and upgrade cadence coupling.

## Consequences

- `west.yml` documents the placeholder; no CHIP project is pinned yet.
- The CoAP networking path can proceed independently of this decision.
- Whoever resolves this ADR must also record the Zephyr↔CHIP version
  pairing policy and CI toolchain implications.
