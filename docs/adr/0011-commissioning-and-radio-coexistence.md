# 0011 — Commissioning strategy and BLE/Thread radio coexistence

- Status: accepted
- Date: 2026-08-04

## Context

Matter's standard onboarding flow requires BLE and Thread to operate
**concurrently only during the commissioning window** (a few minutes:
the BLE session stays open while the device joins Thread and the
commissioner completes the flow over IP). Normal operation and even
multi-admin joining run entirely over Thread — BLE may be off.

On vanilla Zephyr there is no radio arbiter to timeslice BLE and
802.15.4 on Nordic silicon: Nordic's dynamic-multiprotocol layer (MPSL +
SoftDevice Controller, in sdk-nrfxlib) is a permissively licensed but
proprietary binary (Nordic-silicon-only clause) and is integrated only
by the nRF Connect SDK. The Matter prototype (this branch) verified that
a Thread-only Matter node builds and boots on vanilla Zephyr
(nRF5340/nRF7002-DK, netcore running the upstream 802154 serialization
image).

## Decision (product owner, 2026-08-04)

1. **Standard compliance is mandatory.** A reboot-based pseudo-flow
   (provision over BLE, restart into Thread-only) breaks the specified
   commissioning sequence and is **rejected**.
2. **v0.1 commissions on-network, without BLE:** the Thread operational
   dataset is provisioned by MCUHome's own tooling (at flash time via
   USB, later via the dashboard/maintenance channel), and Matter
   commissioning then runs over the operational network. BLE stays
   disabled in v0.1 firmware. Limitation, accepted: v0.1 devices are
   onboarded through MCUHome tooling, not via phone/QR-code BLE flow.
3. **Standard BLE commissioning is added later** by integrating Nordic's
   multiprotocol libraries (sdk-nrfxlib: MPSL + SoftDevice Controller +
   802.15.4 service layer) on top of vanilla Zephyr — **subject to a
   dedicated feasibility and effort analysis before implementation**
   (licensing is compatible; the unknown is integration effort without
   NCS glue).

## Consequences

- v0.1 firmware stays radio-simple (one stack at a time); the
  coexistence problem is downgraded from blocker to a scoped later
  work package.
- The builder/dashboard gain a provisioning responsibility (dataset
  injection) — to be specified with the flashing-UX design.
- Full retail-style Matter onboarding (BLE + QR code) is a prerequisite
  for any claim of out-of-the-box phone commissioning; revisit this ADR
  when that milestone is planned.
