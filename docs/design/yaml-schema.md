# MCUHome YAML Schema — Design

> **Status: approved by the product owner (2026-08-03).** Based on
> ADR 0009 (Matter-explicit, devicetree-aligned) and ADR 0010
> (Matter-only integration, CoAP deferred). Product anchors: Nordic
> nRF52/nRF54 first, environmental sensors first, full declarative
> automation engine. Complete example configurations: [examples/](examples/).

## 1. Principles

1. **One data model.** The Matter data model
   (node → endpoints → clusters → attributes/commands) is the single
   source of truth. Matter exposes it natively; the future CoAP
   maintenance surface derives the *same* structure automatically (§7).
   Nothing is ever modeled twice.
2. **Devicetree-aligned hardware.** The `hardware:` section maps as
   directly as possible to a generated devicetree overlay: compatible
   strings (`sensirion,sht4x`), bus hierarchy, Zephyr pin notation.
3. **No embedded code.** Configs never contain C/C++ snippets (ESPHome
   lambdas are explicitly rejected). Automations are fully declarative
   YAML (§8); if a device needs real code, that is a custom component
   (separate mechanism, later design).
4. **Explicit over implicit.** The builder derives nothing silently that
   the user could want to control; defaults exist but are documented and
   overridable. Validation errors must point at file/line/key.
5. **One file = one device.** Reuse mechanisms (`!secret`, later
   packages/substitutions) are additive, never required.

## 2. Top-level structure

```yaml
device:       # identity & platform
network:      # transports (thread/wifi) and protocols (matter/coap)
hardware:     # buses, peripherals, GPIO — devicetree-shaped
node:         # Matter data model: endpoints, device types, clusters
automations:  # declarative on-device logic
```

All sections except `device:` are optional; a config with only `device:`
must build (a commissionable but featureless node).

## 3. `device:` — identity and platform

```yaml
device:
  name: bedroom-climate            # [a-z0-9-], ≤32 chars; hostname/node id
  friendly_name: Bedroom Climate
  board: nrf54l15dk/nrf54l15/cpuapp  # Zephyr board target, verbatim
  power:
    source: battery                # battery | mains  (default: mains)
  # Advanced (ADR 0013 incl. amendment) — defaults serve the typical user:
  blob_usage: auto                  # auto | none  (default: auto)
  zephyr_version: auto             # auto | <release line, e.g. "4.4"> | latest
  blobs:                           # per-blob overrides (rarely needed)
    nordic-cc3xx: auto             # enabled | disabled | auto (version wins)
```

- `board` is the exact Zephyr board target string — no own board naming
  layer. Validation: against the boards known to the pinned Zephyr.
- `power.source: battery` switches defaults conservatively (sampling
  intervals, logging off, Thread role hint) and activates the Matter
  Power Source cluster (battery level reporting if a measurement source
  is wired — see the `voltage-divider` peripheral in §5).
- Matter vendor/product IDs default to the Matter test VID/PID (fine for
  DIY/commissioning; real IDs are a product-owner topic far later).
- `blob_usage`/`zephyr_version`/`blobs` implement the blob policy and
  per-device Zephyr pinning of ADR 0013 (incl. per-blob amendment).
  Default resolution: blobs are hard constraints and drive the automatic
  Zephyr pin. A per-blob `auto` inverts that priority (version wins,
  blob used iff compatible — self-healing when the vendor catches up).
  Impossible forced combinations are rejected at validation time with a
  plain-language recommendation and a copy-paste snippet; the builder
  never flips functional trade-offs implicitly. A recommendation-drift
  check flags override lines that have become redundant.

## 4. `network:` — transports and protocols

Transports (physical connectivity) and protocols (application layer) are
separate keys, because they combine freely (Matter-over-Thread,
Matter-over-WiFi, CoAP-over-either):

```yaml
network:
  thread:
    device_role: sed               # ftd | mtd | sed | ssed
    poll_interval: 5s              # SED slow-poll (ICD slow interval)
  # wifi:                          # Nordic: requires nRF70-series companion
  #   ssid: !secret wifi_ssid
  #   password: !secret wifi_password

  matter:
    enabled: true                  # default; shown for clarity
```

- Exactly one transport must be configured; a board without radio for it
  is a validation error.
- **Matter is the integration path** (ADR 0010): enabled by default
  whenever a transport is configured; can be disabled for bench setups
  (automations still run locally).
- `coap:` is a **reserved key** for the future maintenance channel
  (§7). The v1 builder rejects it with "not yet implemented" — never
  silently ignores it.
- Thread credentials come via Matter commissioning, not from the YAML
  (secrets never end up in git-managed configs; `!secret` covers the
  rest).
- `device_role: sed` above is target schema, not current capability: the
  runtime offers Router (FTD) and Minimal End Device (MED) only today.
  SED support is not offered yet — it lands with the power-management
  phase (ICD configuration, poll period, Matter LIT/SIT semantics).

## 5. `hardware:` — devicetree-shaped hardware description

```yaml
hardware:
  buses:
    i2c0:                          # instance name = key
      controller: i2c21            # optional: pick a specific SoC controller
      sda: gpio1.11                # Zephyr notation: port.pin
      scl: gpio1.12
      frequency: 400khz

  peripherals:
    climate_chip:                  # user-chosen id, referenced elsewhere
      driver: sensirion,sht4x      # devicetree compatible string, verbatim
      bus: i2c0
      address: 0x44

    status_led:
      driver: gpio-led
      pin: gpio0.9
      active_low: true

    buzzer:
      driver: gpio-output          # generic switched output — not a "led"
      pin: gpio0.10

    battery_voltage:
      driver: voltage-divider      # Zephyr's real binding for battery sensing
      adc:
        channel: 4
      divider: [1500k, 180k]
```

- **Exactly two categories, no special cases:** `buses:` is shared
  infrastructure that peripherals reference; `peripherals:` is
  everything with behavior, each carrying a `driver:`. A bare GPIO pin
  is not a category of its own — the driver gives it semantics, exactly
  as devicetree thinks.
- **Driver naming rule:** real chips use their devicetree vendor
  compatible **verbatim** (`sensirion,sht4x`); generic circuit elements
  (LED, button, relay, buzzer, voltage divider) use honest *function
  drivers*: `gpio-led`, `gpio-output`, `gpio-key`, `voltage-divider`.
  Where Zephyr has a standard binding we map to it; where it does not
  (e.g. a generic switched output), MCUHome ships its own binding under
  `dts/bindings/` — normal Zephyr practice. The schema never labels a
  part as something it is not.
- Supported drivers = the intersection of Zephyr's driver base and
  MCUHome's component layer; validation lists the supported set per
  release.
- Peripherals expose **channels** named after Zephyr's sensor channel
  model: `climate_chip.temperature`, `climate_chip.humidity`,
  `battery_voltage.voltage`. These channel references are the universal
  "wire" used by `node:` and `automations:`.
- Everything here compiles to a devicetree overlay (buses, nodes,
  properties) plus Kconfig selections — mechanically, no magic.

## 6. `node:` — the Matter data model, explicit

```yaml
node:
  endpoints:
    - id: 1
      alias: temperature           # optional, for automations/CoAP paths
      device_type: temperature_sensor        # Matter 0x0302
      clusters:
        temperature_measurement:
          source: climate_chip.temperature
          sampling: 60s
          report:
            delta: 0.2             # report when value moved ≥0.2 °C
            max_interval: 15min    # heartbeat report

    - id: 2
      device_type: humidity_sensor           # Matter 0x0307
      clusters:
        relative_humidity_measurement:
          source: climate_chip.humidity
          sampling: 60s
```

- `id` ≥ 1, unique; endpoint 0 (Matter root: basic info, power source,
  ICD management, OTA) is generated automatically — users never write it.
- `device_type` names follow the Matter Device Library, snake_cased; the
  builder validates required-cluster completeness per device type and
  fills mandatory boilerplate attributes with sane defaults.
  (Note: `power.source: battery` in `device:` reports battery level via
  the Power Source cluster when wired to a `voltage-divider` peripheral,
  see §5.)
- `sampling` = how often hardware is read (drives SED wake budget);
  `report` = when a new value is *published* (maps to Matter reporting
  and CoAP Observe notifications alike).
- Actuator clusters use `target:` instead of `source:`
  (e.g. `on_off: { target: status_led }`) — same wiring concept in the
  opposite direction.
- Initial device-type set (v0.1 scope, per product anchors):
  `temperature_sensor`, `humidity_sensor`, `pressure_sensor`,
  `air_quality_sensor`, `light_sensor`, `contact_sensor`, plus
  `on_off_light` / `on_off_plug_in_unit` for the actuator side that the
  automation engine needs. Everything else follows with components.

## 7. Device-to-device and the future CoAP maintenance channel

**Device-to-device communication is served by Matter itself** (ADR 0010):

- **Bindings:** an endpoint with a client cluster (e.g. a switch) is
  bound to a server cluster on another node (e.g. a relay) and then sends
  commands **peer-to-peer through the mesh** — the controller (HA) is
  involved only when *configuring* the binding, never in the data path.
  Survives controller outage.
- **Groups:** one groupcast command reaches many devices at once
  ("room off"). Group membership/keys are provisioned by the controller.
- Node-to-node attribute reads/subscribes are part of Matter's
  interaction model (ACL-gated) — e.g. a thermostat consuming a remote
  temperature sensor.
- Schema impact: none in v1. A future revision extends automations with
  remote targets/sources that compile down to bindings/subscriptions
  (reserved: `bindings:` under `node:`).

**CoAP maintenance channel (deferred, ADR 0010):** OpenThread already
ships a CoAP API (Thread's own network management uses CoAP), so a later
maintenance surface is cheap on our targets. When it comes, it is
**generated from the node model** — no second model, no own config beyond
`network.coap`: resource paths mirror the data model
(`/ep/1/temperature_measurement/measured_value`), discovery via
CoRE Link Format, payloads CBOR, notifications via CoAP Observe,
DTLS-PSK security. Intended for dashboard diagnostics, logs and data
that falls outside Matter's device library — explicitly **not** an
HA integration path.

## 8. `automations:` — declarative on-device logic

Product decision: full engine. The schema defines the complete model;
the implementation phases in (v0.1 implements the subset the examples
use; unsupported constructs fail validation with "not yet implemented",
never silently).

```yaml
automations:
  - id: co2_alarm
    triggers:
      - attribute: co2.measured_value        # alias- or channel-based
        above: 1500
        for: 30s                             # must hold this long
    conditions:
      - attribute: temperature.measured_value
        below: 30
    actions:
      - command: status_led.on_off.on
      - delay: 10min
      - command: status_led.on_off.off
```

Model:

- **Triggers** (any fires → automation runs): attribute thresholds
  (`above/below/equals`, optional `for:`), `changed:`, `interval:`,
  `boot:`, Matter/CoAP command received (device as scene target), later
  button events.
- **Conditions** (all must hold; `any:`/`not:` combinators exist).
- **Actions** (run sequentially): `command:` (invoke cluster command),
  `set:` (write attribute), `delay:`, `log:`, later `scene:`.
- References use `alias.cluster.attribute` (node view) or
  `peripheral.channel` (hardware view) — both resolve to the same value
  plumbing.
- **Deliberately absent:** free-form expressions/templates. v1 offers
  comparisons, thresholds and durations only. An expression language is
  the single biggest complexity driver in this space — reserved as an
  explicit extension point (`expression:` key) so adding it later is
  non-breaking. Anything beyond that is custom-component territory.
- Automations run on-device and keep working without network — with
  `network:` absent entirely, a config degrades to a standalone
  automation controller.
- **Future:** remote triggers/actions across devices compile down to
  Matter bindings/subscriptions (§7) — same syntax, remote reference;
  reserved, not in v1.

## 9. Secrets and includes

- `!secret key_name` reads from `secrets.yaml` next to the config
  (ESPHome-identical UX, deliberately familiar).
- `!include` / packages / substitutions: reserved for a later schema
  revision; the design keeps top-level keys merge-friendly for it.

## 10. Validation & versioning

- The schema is versioned with MCUHome (SemVer); breaking schema changes
  = breaking release. No `schema_version:` key in v1 (YAGNI — the
  builder knows its own version; revisit before 1.0).
- Validation layers: YAML syntax → schema shape → cross-references
  (unknown channel/alias/bus) → platform capability (board has no radio,
  I2C controller collision) → Matter conformance (device type requires
  cluster X). Each error carries file/line and a fix hint.

## 11. Open points (deliberately deferred)

| Topic | Status |
|---|---|
| Custom components (user C code) | Own design doc, after builder pipeline |
| OTA update flow details | Own design doc (Matter OTA) |
| CoAP maintenance channel | Deferred (ADR 0010); derived surface, see §7 |
| Cross-device automations (bindings) | Reserved schema extension, see §7/§8 |
| Packages/substitutions/includes | Schema revision 2 |
| Expression language in automations | Reserved extension point |
| Real Matter VID/PID | PO topic, pre-certification |
