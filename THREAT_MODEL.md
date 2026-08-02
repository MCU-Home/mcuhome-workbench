# Threat Model

> **Status: stub.** A full threat model (STRIDE-based) will be developed
> during the architecture design phase, before the first functional release.
> This file records the initial asset inventory so security is designed in,
> not bolted on.

## Assets

| Asset | Where it lives | Impact if compromised |
|---|---|---|
| Thread network key | Device flash / secure storage (PSA ITS) | Full Thread network compromise |
| Matter fabric credentials (NOC, ICAC) | Device secure storage | Device impersonation, fabric access |
| WiFi credentials | Device flash / provisioning | Network access |
| Firmware images & OTA path | Builder output, update channel | Persistent device takeover |
| Builder toolchain | User machine / dashboard host | Supply-chain compromise of all built devices |
| YAML device configurations | User storage, dashboard | Credential leakage, malicious rebuilds |

## Trust boundaries (to be elaborated)

- Device ↔ local network (CoAP/DTLS, Matter over Thread/WiFi)
- Builder ↔ third-party dependencies (Zephyr, modules, Matter SDK — pinned
  revisions in `west.yml`)
- Dashboard ↔ browser ↔ builder backend

## Out of scope (for now)

- Physical attacks requiring chip decapping
- Attacks on Home Assistant or other Matter controllers themselves
