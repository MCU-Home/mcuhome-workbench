# 0009 — Matter-explicit YAML schema, aligned with devicetree conventions

- Status: accepted
- Date: 2026-08-03

## Context

Three options were evaluated for the device configuration schema:

- **(a) ESPHome-compatible.** Rejected: real-world ESPHome configs embed
  C++ lambdas against ESPHome's runtime API (unrunnable on Zephyr), their
  `api:`/`ota:`/board blocks encode an architecture we replace with
  Matter, and a compatibility promise would couple us to ESPHome's
  monthly schema evolution forever. Partial compatibility would break on
  the average config and cost more trust than it buys.
- **(b) Own schema with ESPHome-familiar idioms.** Component blocks
  (`sensor:` …) with the Matter structure derived automatically by the
  builder; optional per-component overrides.
- **(c) Matter-explicit schema.** The config visibly mirrors the Matter
  data model — node → endpoints → clusters — with a separate `hardware:`
  section; the user wires hardware sources to clusters explicitly.

Product owner decision: **(c)**, with the explicit addition that the
schema should also lean on Zephyr devicetree conventions. Rationale: the
explicit form reads naturally for users who already know Zephyr and
devicetree — the trade-off of a steeper entry for ESPHome migrants is
accepted deliberately.

## Decision

- The YAML schema mirrors the **Matter data model explicitly**: a `node:`
  section declares endpoints, each with a Matter device type and its
  clusters; hardware→cluster wiring is explicit (`source:` references).
- The `hardware:` section follows **devicetree conventions** wherever
  sensible: driver identifiers are devicetree compatible strings
  (e.g. `sensirion,sht4x`), bus/peripheral structure mirrors the
  devicetree hierarchy (buses with child devices), and pin/node naming
  stays close to Zephyr nomenclature. YAML→devicetree-overlay generation
  should be as direct a mapping as possible.
- Non-Matter transports (plain CoAP) reuse the same explicit structure;
  details are part of the schema design phase.
- The planned ESPHome **migration tool** (later milestone) translates
  ESPHome configs *into* this explicit schema, with an honest report of
  what cannot be translated (lambdas, custom components).

## Consequences

- Steeper learning curve for ESPHome migrants; mitigated by documentation,
  commented examples and the migration tool — not by schema magic.
- Zephyr-literate users get a schema that matches their mental model;
  the YAML→devicetree/Kconfig mapping in the builder stays simple and
  debuggable (what you write is what gets generated).
- Full control over endpoint composition from day one (composed devices,
  bridges, certification-grade layouts) without override mechanisms.
- Convenience layers (templates/packages for common device patterns) may
  be added later on top, but the explicit form remains the canonical
  representation.
- Working examples from this decision live at workspace level
  (`schema-beispiele/`, untracked); the binding schema definition is
  produced in the design phase.
