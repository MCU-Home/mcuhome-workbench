# 0012 — Device attestation (DAC) strategy for user-built devices

- Status: draft
- Date: 2026-08-04

## Context

Every Matter commissioning includes an attestation step: the device
presents a **DAC** (Device Attestation Certificate) and the commissioner
validates its chain against a PAA registered in the CSA's **DCL**
(Distributed Compliance Ledger). Controllers reject devices whose chain
does not resolve.

Matter's attestation model assumes **fixed series products**: a vendor
certifies a device model (CSA membership plus per-model certification
fees) and provisions a unique DAC into every unit at manufacturing time.

MCUHome contradicts that assumption by design — every user YAML produces
a different device (endpoints, clusters, hardware). There is no fixed
model to certify, so classic certification is not merely expensive, it
is structurally inapplicable to user-built devices. This is a property
of Matter, not of MCUHome: it affects every DIY Matter solution.

The E2E commissioning test (2026-08-04) hit this concretely — Home
Assistant refused the prototype until "test-net DCL" was enabled.

## Decision (product owner, 2026-08-04)

**Path A now, path B as the target for v1.0.**

### A — Test certificates (current state, documented starting path)

Devices use the public CHIP SDK test DAC (test vendor ID range). Users
enable their controller's test-certificate option once (Home Assistant:
Matter Server add-on → "Enable test-net DCL usage").

- Pro: zero infrastructure on our side; works with every controller that
  offers the switch.
- Con: the switch is **global** — it lowers attestation checking for all
  devices on that controller, not just MCUHome ones. Acceptable for a
  development/alpha phase, not a good permanent answer.

### B — MCUHome's own attestation root (target for v1.0)

MCUHome operates its own PAA; the builder issues a per-device DAC at
build time (device identity comes from the config anyway). Users install
**one MCUHome root certificate** into their controller once.

- Pro: a scoped, explicit "I trust MCUHome" decision instead of a
  blanket switch — the same mental model as trusting a self-signed CA in
  a home network. Devices stay individually identifiable.
- Con: only works with controllers that accept additional roots (Home
  Assistant does; Apple/Google ecosystems do not). Those fall back to A.
- Open questions for the implementation phase: root key custody (the
  private key must never live in the repo or in CI), builder-side
  certificate generation, secure per-device key storage on the MCU, and
  the user-facing installation step.

### C — Real certification (out of scope)

Only relevant if someone ships pre-built MCUHome hardware as a product;
it can never cover user-composed devices.

## Consequences

- Documentation must state plainly that self-built Matter devices have
  no certified attestation path, and what the test-certificate switch
  actually relaxes — no glossing over the security trade-off.
- The builder gains a certificate-provisioning stage (path B); the
  device model must carry attestation material.
- Deferred until the builder implementation phase; revisit if the CSA
  ever introduces a DIY/self-attestation tier.
