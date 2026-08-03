# 0010 — Matter-only integration; CoAP deferred to a maintenance channel

- Status: accepted
- Date: 2026-08-03

## Context

The original product idea considered CoAP as the general application
protocol, including the Home Assistant integration and direct
device-to-device packets (in the spirit of ESPHome's ESP-NOW/packet
transport component). Analysis for the schema design showed:

- For controller integration, CoAP buys nothing over Matter but costs a
  custom Home Assistant integration — exactly the ecosystem dependency
  the Matter decision was meant to eliminate.
- The device-to-device requirement is covered by Matter natively:
  **bindings** (peer-to-peer cluster commands through the mesh, the
  controller only configures the binding and is not in the data path),
  **groups** (groupcast to many nodes), and ACL-gated node-to-node
  reads/subscriptions in the interaction model.
- Matter does not use CoAP internally (its own message format over UDP),
  but Thread's network management does, so OpenThread ships a CoAP API —
  a later CoAP channel is cheap on our targets.
- For the v0.1 scope (environmental sensors on Nordic, Thread incl.
  SED), Matter's device library has no functional gap.

## Decision

- **v0.1 is Matter-only.** Matter is the sole integration path for Home
  Assistant and other controllers. No custom HA integration is built.
- **Device-to-device uses Matter bindings/groups.** The schema reserves
  an extension for cross-device automations that compile down to
  bindings/subscriptions.
- **CoAP is repositioned, not dropped:** a future maintenance/diagnostics
  channel (dashboard access, logs, data outside Matter's device library),
  derived automatically from the node data model — never a parallel
  integration path. `network.coap` stays a reserved schema key; the v1
  builder rejects it as "not yet implemented".

This supersedes the earlier "strictly parallel protocols" product anchor
from the schema kickoff (which never became an ADR).

## Consequences

- v0.1 scope shrinks substantially: one protocol stack, one security
  story, one test matrix; the "custom HA integration" deliverable
  disappears entirely.
- Data that does not fit Matter's device library is not remotely
  accessible until the CoAP maintenance channel exists (local automations
  can still use it).
- Cross-device behavior depends on Matter commissioning (fabric
  membership) — acceptable: standalone-without-controller operation is a
  niche we consciously deprioritize.
- When the CoAP channel is designed, it inherits the derived-surface
  principle from the schema design (one data model, no second config).
