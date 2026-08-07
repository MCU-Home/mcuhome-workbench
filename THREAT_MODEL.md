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
| Commissioning credentials (passcode, salt, SPAKE2+ verifier) | Device YAML / `secrets.yaml`, compiled into the firmware image | Anyone can commission the device into their own fabric |
| Firmware images & OTA path | Builder output, update channel | Persistent device takeover |
| Builder toolchain | User machine / dashboard host | Supply-chain compromise of all built devices |
| YAML device configurations | User storage, dashboard | Credential leakage, malicious rebuilds |

## Trust boundaries (to be elaborated)

- Device ↔ local network (CoAP/DTLS, Matter over Thread/WiFi)
- Builder ↔ third-party dependencies (Zephyr, modules, Matter SDK — pinned
  revisions in `west.yml`)
- Dashboard ↔ browser ↔ builder backend

Note on the commissioning credentials: vanilla Zephyr CHIP has no
factory-data mechanism, so they are Kconfig values baked into the image
(one image per device). A 27-bit passcode recovered from a stolen
verifier is a matter of GPU time, which makes a built image as sensitive
as the configuration it came from. The per-device random salt MCUHome
generates removes the cross-device attack (one precomputed table for
every device, IACR 2025/1268); it does not make a single leaked image
safe.

## Out of scope (for now)

- Physical attacks requiring chip decapping
- Attacks on Home Assistant or other Matter controllers themselves
