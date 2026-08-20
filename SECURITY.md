# Security Policy

## Supported versions

MCUHome is pre-alpha and has no released versions yet. This policy takes
full effect with the first release; until then, reports about the scaffold
and build tooling are still welcome.

## Reporting a vulnerability

**Do not open public issues for security vulnerabilities.**

Use GitHub's private vulnerability reporting:
**Security → Report a vulnerability** on this repository
([direct link](https://github.com/mcu-home/mcuhome-workbench/security/advisories/new)).

We aim to acknowledge reports within **3 business days**. Please include a
description of the issue, affected component (runtime, builder, generated
firmware), and reproduction steps where possible.

## Scope

Particularly relevant attack surfaces for this project:

- Device credentials: Thread network keys, Matter fabric credentials,
  WiFi credentials embedded in or provisioned to devices.
- Integrity of generated firmware images and the OTA update path.
- The builder toolchain as a supply chain (YAML processing, codegen,
  dependency pinning).

See also [THREAT_MODEL.md](THREAT_MODEL.md).
