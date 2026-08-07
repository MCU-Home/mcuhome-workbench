# 0014 — Generated-tables contract and native composed-node topology

- Status: accepted
- Date: 2026-08-05

## Context

Builder phase 2 generates the per-device firmware configuration that
phase 1 (schema, pipeline, component model) designed around. The
builder's core design principle — "thin codegen + static tables"
([builder-pipeline.md](../design/builder-pipeline.md) §1) — requires
generated code to be dumb data: reviewable by a human, diffable, and
stable across Matter SDK (CHIP) version bumps without regeneration.

The E2E prototype (ADR 0006; findings in
[matter-zephyr-integration.md](../design/matter-zephyr-integration.md),
Addendum 3) proved the core mechanism — dynamic endpoint registration
at runtime — compiles and commissions on real hardware against a real
controller. But the prototype app itself does not meet the phase-2
contract:

- It calls CHIP/ember APIs and includes CHIP/ember headers directly
  from application code (`emberAfSetDynamicEndpoint`, external
  attribute storage callbacks). None of that is data a builder could
  safely generate per device config.
- It inherited its endpoint topology from CHIP's `bridge-app` ZAP
  template (the only working starting point at prototype time): a
  static aggregator on endpoint 1, with the dynamically registered
  sensor endpoint placed at endpoint 2 underneath it. MCUHome devices
  are not bridges — they are single physical devices exposing their
  own clusters — so this topology is structurally wrong, and it
  surfaced as a visible bug: Home Assistant showed the node's device
  type as "Dimmable Light" and its name as "not-specified", because
  the aggregator's Descriptor cluster data does not describe the
  actual device.

Both problems trace back to the same root cause — copying prototype
shortcuts forward instead of defining the generated/framework boundary
and the endpoint topology deliberately. This ADR fixes both before
builder phase 2 codegen is designed against them.

## Decision

### A. Plain-C tables contract, versioned

Generated device configuration contains **zero CHIP/ember includes**.
It emits exactly one symbol, `const struct mcuhome_matter_node
mcuhome_node_config`, built from plain-C structs defined once in
`include/mcuhome/matter_tables.h`:

- `mcuhome_matter_node` — `{ tables_version, endpoints[] }`.
- `mcuhome_matter_endpoint` — `{ endpoint_id (>= 1), parent_id (0 =
  directly under root), device_types[], clusters[] }`.
- `mcuhome_matter_cluster` — `{ id, feature_map, cluster_revision,
  attrs[] }`. Never the Descriptor cluster — the framework appends it
  automatically (see Decision B).
- `mcuhome_matter_attr` — `{ id, type enum, size, flags
  (writable/nullable), pointer to a RAM attr-store cell or a constant
  default }`.

The framework serves the global attributes FeatureMap (0xFFFC) and
ClusterRevision (0xFFFD) of every table-registered cluster from the
`feature_map`/`cluster_revision` fields — table authors and the
builder never declare them as attrs. This closes a real conformance
gap found by source analysis of CHIP v1.5.1.0: the prototype returns
UNSUPPORTED_ATTRIBUTE for FeatureMap and FAILURE for ClusterRevision
on its dynamic cluster (controllers such as Home Assistant happen not
to read either, which is why E2E passed).

A `MCUHOME_MATTER_TABLES_VERSION` define is checked at framework init;
any contract change bumps it, so a stale generator/framework pairing
fails loudly instead of misbehaving silently.

The framework (`components/matter`) owns **all** translation to ember
structures: metadata pool sizing via `CONFIG_MCUHOME_MATTER_MAX_*`
Kconfig symbols, the single external attribute read/write callback
pair, DataVersions, and every other CHIP API call. App and channel
code (component model, `component-model.md` §5) only writes attr-store
cells and calls `mcuhome_matter_attr_changed(ep, cluster, attr)`; it
never touches CHIP headers or CHIP's locking.

The hand-written table set of the phase-1 sample
(`samples/matter-node/`) becomes the golden file for builder phase-2
codegen tests, per the golden-file testing strategy already fixed in
`builder-pipeline.md` §9.

### B. Native composed-node topology

MCUHome devices are standard composed nodes, not bridges. The
framework ships its own minimal ZAP file (`mcuhome-root.zap`):
endpoint 0 only, device type 0x0016 (root node). The `bridge-app`
template and its aggregator endpoint are dropped entirely. Dynamic
endpoints registered from the tables above start at EP1, directly
under root — no intermediate aggregator.

Descriptor data (DeviceTypeList / ServerList / PartsList) is derived
exclusively from the registered tables, never hand-authored in ZAP. A
compile-time assert forbids fixed endpoints other than EP0, so
`mcuhome-root.zap` can never reintroduce a ghost endpoint by accident.

Mechanism (verified against CHIP v1.5.1.0 source): the Descriptor
cluster is code-driven (`ServerClusterInterface`) there — its values
always come from the endpoint/cluster metadata, but the cluster is
only *instantiated* for endpoints whose registered cluster list
contains a Descriptor server entry. The framework therefore appends a
minimal Descriptor cluster entry (no ZAP-authored list attributes —
only the automatically provided ClusterRevision slot; the four list
attributes the prototype declared are vestigial
[correction, 2026-08-06: this originally said "empty attribute list",
which was factually wrong — the auto-appended entry carries exactly one
declared attribute, `MCUHOME_MATTER_DESCRIPTOR_ATTR_COUNT` = 1]) to
every dynamic endpoint, and derives the registry sizing
(`CHIP_DEVICE_CONFIG_DYNAMIC_ENDPOINT_COUNT`) from the same Kconfig
symbol that sizes its own pools, because an undersized registry drops
Descriptor registration silently upstream.

Consequence for the existing prototype device: one-time
re-commissioning, moving the sensor endpoint from EP2 to EP1 with the
corrected device type (0x0302, temperature sensor, instead of the
inherited "Dimmable Light" type).

## Consequences

- Builder phase-2 codegen targets a stable, versioned plain-C contract
  insulated from CHIP SDK churn: a CHIP version bump requires
  re-verifying the framework's ember translation layer, not
  regenerating or re-validating device configs.
- Automation tables and actuator write-path semantics are explicitly
  **out of scope for contract v1** — both remain open points in
  `component-model.md` §9 and get their own tables/version bump later.
- The phase-1 sample's golden tables double as the phase-2 codegen
  regression fixture, so the sample must be kept in lockstep with the
  contract from here on.
- The dev prototype requires one manual re-commissioning after the
  topology change; no other user-facing devices exist yet, so this
  cost is paid once and does not recur.
- Related standing decisions: ADR 0006 (upstream CHIP as the SDK whose
  types this contract shields the app from) and ADR 0009 (the
  Matter-explicit YAML schema this contract is the compile target of).

## Amendment: channel bindings in the same generated file (2026-08-07)

Builder stage 4 surfaced an under-description in Decision A: "exactly
one symbol" describes the **Matter tables** contract, whose sole symbol
remains `mcuhome_node_config`. The generated configuration file
additionally carries the sensor-channel bindings
(`mcuhome_sensor_bindings[]` / `mcuhome_sensor_binding_count`, structs
from `include/mcuhome/channel.h`) — device configuration exactly like
the tables, emitted from the same YAML, but governed by the channel
layer's own contract, not this one. Future automation tables (out of
scope for contract v1, see above) follow the same pattern: one
generated file per device, one symbol set per contract domain.
