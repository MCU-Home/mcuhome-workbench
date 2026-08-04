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

**Upstream `project-chip/connectedhomeip` (pinned v1.5.1.0), no Nordic
fork.** The integration prototype (see
[docs/design/matter-zephyr-integration.md](../design/matter-zephyr-integration.md))
proved compile AND runtime viability on vanilla Zephyr v4.4.0
(nRF5340/nRF7002-DK, Thread-only per ADR 0011): the required delta is a
small, fully documented patch set ([patches/](../../patches/)) plus
configuration defaults the builder will own. Nordic's proprietary
multiprotocol libraries become relevant only for the later BLE
commissioning milestone (ADR 0011) and will be evaluated then as an
addition on top of vanilla Zephyr — not as a fork of it.

## Consequences

- `west.yml` pins connectedhomeip v1.5.1.0 (matter group, explicit
  submodules); Zephyr and CHIP pins are bumped as a version-matched
  pair, validated by the integration test that CI will grow from the
  prototype findings.
- The patch set in `patches/` must be applied on top of the pinned
  checkouts until hunks are upstreamed (candidates tracked outside the
  repo); the builder automates this.
- CHIP's GN build adds gn + zap-cli as build-time tools — provisioned
  by the builder image, never by contributors manually.
