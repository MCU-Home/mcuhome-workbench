# 0014 — Generated-tables contract and native composed-node topology

- Status: accepted
- Date: 2026-08-05
- Finalized: 2026-08-14

## Context

Phase 2 of the build pipeline generates the per-device firmware
configuration that phase 1 (schema, pipeline, component model) designed
around. The pipeline's core design principle — "thin codegen + static
tables" ([builder-pipeline.md](../design/builder-pipeline.md) §1) —
requires generated code to be dumb data: reviewable by a human,
diffable, and stable across Matter SDK (CHIP) version bumps without
regeneration.

The E2E prototype (ADR 0006; findings in
[matter-zephyr-integration.md](../design/matter-zephyr-integration.md),
Addendum 3) proved the core mechanism — dynamic endpoint registration
at runtime — compiles and commissions on real hardware against a real
controller. But the prototype app itself did not meet the phase-2
contract:

- It called CHIP/ember APIs and included CHIP/ember headers directly
  from application code (`emberAfSetDynamicEndpoint`, external
  attribute storage callbacks). None of that is data a generator could
  safely emit per device config.
- It inherited its endpoint topology from CHIP's `bridge-app` ZAP
  template (the only working starting point at prototype time): a
  static aggregator on endpoint 1, with the dynamically registered
  sensor endpoint placed at endpoint 2 underneath it. MCUHome devices
  are not bridges — they are single physical devices exposing their
  own clusters — so this topology was structurally wrong, and it
  surfaced as a visible bug: Home Assistant showed the node's device
  type as "Dimmable Light" and its name as "not-specified", because
  the aggregator's Descriptor cluster data did not describe the
  actual device.

Both problems trace back to the same root cause — copying prototype
shortcuts forward instead of defining the generated/framework boundary
and the endpoint topology deliberately. This ADR fixed both before
phase-2 codegen was designed against them.

## Decision

### A. Plain-C tables contract, versioned

Generated device configuration contains **zero CHIP/ember includes**.
Its Matter data model is exactly one symbol, `const struct
mcuhome_matter_node mcuhome_node_config`, built from plain-C structs
defined once in `include/mcuhome/matter_tables.h`:

- `mcuhome_matter_node` — `{ tables_version, endpoints[] }`.
- `mcuhome_matter_endpoint` — `{ endpoint_id (>= 1), parent_id (0 =
  directly under root), device_types[] (id + revision), clusters[] }`.
- `mcuhome_matter_cluster` — `{ id, feature_map, cluster_revision,
  attrs[] }`. Never the Descriptor cluster — the framework appends it
  automatically (see Decision B).
- `mcuhome_matter_attr` — `{ id, type enum, wire size, flags
  (writable/nullable), store, def }`. `store` points at a RAM cell
  (`struct mcuhome_attr_store`), or is NULL for a constant attribute
  the framework always serves from `def` — which is how fixed cluster
  metadata such as MinMeasuredValue / MaxMeasuredValue is expressed
  without spending RAM on it.

The tables are `const` and live in flash; the framework never writes
them. Store cells are RAM, owned by application/channel code, and
written only through the header's `mcuhome_attr_store_publish_*()` /
`mcuhome_attr_store_invalidate()` helpers: a cell is written by an
application thread and read by the CHIP thread with no other
synchronization between them, and the helpers order the two with
release/acquire atomics on the cell's `valid` flag. The helpers joined
contract v1 without a version bump — a deliberate call while the
project is pre-release and no consumer of the header exists outside
this tree — but from that point on the helpers, not the struct fields,
are the contract for touching a cell.

The **global attribute range 0xFFF8–0xFFFD is framework-owned**: table
authors and the generator never declare a global attribute. FeatureMap
(0xFFFC) and ClusterRevision (0xFFFD) are served by the framework from
the cluster's `feature_map`/`cluster_revision` fields; the
attribute/command lists (0xFFF8–0xFFFB) are appended by CHIP's
data-model provider itself, so declaring them in the tables would
duplicate wire-visible list entries. The FeatureMap/ClusterRevision
half of this rule closes a real conformance gap found by source
analysis of CHIP v1.5.1.0: the prototype returned
UNSUPPORTED_ATTRIBUTE for FeatureMap and FAILURE for ClusterRevision
on its dynamic cluster (controllers such as Home Assistant happen not
to read either, which is why E2E passed).

A `MCUHOME_MATTER_TABLES_VERSION` define is checked at framework init;
any contract change bumps it, so a stale generator/framework pairing
fails loudly instead of misbehaving silently.

The framework (`components/matter`) owns **all** translation to ember
structures: metadata pool sizing via `CONFIG_MCUHOME_MATTER_MAX_*`
Kconfig symbols, the single external attribute read/write callback
pair, DataVersions, and every other CHIP API call. Contract validation
itself is CHIP-free by construction and lives in its own translation
unit (`components/matter/src/table_validate.c`), apart from the
CHIP-coupled `endpoint_registry.cpp` — which is what lets the
validator run, and be tested, on `native_sim`, where CHIP cannot
build. App and channel code (component model,
[component-model.md](../design/component-model.md) §5) only writes
attr-store cells and calls `mcuhome_matter_attr_changed(endpoint_id,
cluster_id, attr_id)`; it never touches CHIP headers or CHIP's
locking.

"Exactly one symbol" scopes the **Matter tables** contract, whose sole
symbol is `mcuhome_node_config`. The generated configuration file
additionally carries the sensor-channel bindings
(`mcuhome_sensor_bindings[]` / `mcuhome_sensor_binding_count`, structs
from `include/mcuhome/channel.h`) — device configuration exactly like
the tables, emitted from the same YAML, but governed by the channel
layer's own contract, not this one. Future automation tables (out of
scope for contract v1, see Consequences) follow the same pattern: one
generated file per device, one symbol set per contract domain.

The table set of the phase-1 sample (`samples/matter-node/`) is the
codegen regression fixture, per the golden-file testing strategy fixed
in [builder-pipeline.md](../design/builder-pipeline.md) §9. The
direction of that fixture has since inverted: originally the
hand-written phase-1 tables were the golden file the generator had to
reproduce; today `samples/matter-node/src/mcuhome_config.{c,h}` **is
generator output**, committed, and `tests_py/test_generate.py`
compares it byte for byte against fresh output. Either way around, the
comparison is what keeps sample and contract in lockstep.

### B. Native composed-node topology

MCUHome devices are standard composed nodes, not bridges. The
framework ships its own minimal ZAP file
(`components/matter/zap/mcuhome-root.zap`): endpoint 0 only, device
type 0x0016 (root node). The `bridge-app` template and its aggregator
endpoint are dropped entirely. Dynamic endpoints registered from the
tables above start at EP1, directly under root — no intermediate
aggregator.

Descriptor data (DeviceTypeList / ServerList / PartsList) is derived
exclusively from the registered tables, never hand-authored in ZAP. A
`static_assert` in `matter_init.cpp` pins CHIP's
`FIXED_ENDPOINT_COUNT` to 1, so `mcuhome-root.zap` can never
reintroduce a ghost endpoint by accident.

Mechanism (verified against CHIP v1.5.1.0 source): the Descriptor
cluster is code-driven there (`ServerClusterInterface`) — its values
always come from the endpoint/cluster metadata, but the cluster is
only *instantiated* for endpoints whose registered cluster list
contains a Descriptor server entry. The framework therefore appends a
minimal Descriptor cluster entry to every dynamic endpoint, carrying
exactly one declared attribute — the automatically provided
ClusterRevision slot (`MCUHOME_MATTER_DESCRIPTOR_ATTR_COUNT` = 1); the
four list attributes a hand-authored ZAP would declare are vestigial
and never declared. It also derives the registry sizing
(`CHIP_DEVICE_CONFIG_DYNAMIC_ENDPOINT_COUNT`) from
`CONFIG_MCUHOME_MATTER_MAX_DYNAMIC_ENDPOINTS`, the same Kconfig symbol
that sizes its own pools, because an undersized registry drops
Descriptor registration silently upstream — and the SDK default of 0
makes every `emberAfSetDynamicEndpoint()` fail with
`CHIP_ERROR_NO_MEMORY`.

Consequence for the then-existing prototype device: a one-time
re-commissioning, moving the sensor endpoint from EP2 to EP1 with the
corrected device type (0x0302, temperature sensor, instead of the
inherited "Dimmable Light" type). The reference sample has since grown
a second table-registered endpoint under root (EP1 temperature 0x0302,
EP2 pressure 0x0305) — the composed topology this decision fixed,
exercised with more than one endpoint.

## Consequences

- Phase-2 codegen (today stage 4, `mcuhome/compiler/generate.py`)
  targets a stable, versioned plain-C contract insulated from CHIP SDK
  churn: a CHIP version bump requires re-verifying the framework's
  ember translation layer, not regenerating or re-validating device
  configs.
- Because the contract and its validator are CHIP-free by design,
  every twister suite in `tests/` runs without the `matter` west
  group; CI covers the CHIP-coupled build path with a separate Matter
  job, precisely because this exclusion would otherwise leave it
  unexercised.
- Automation tables and actuator write-path semantics are explicitly
  **out of scope for contract v1** — both remain open points in
  [component-model.md](../design/component-model.md) §9 and get their
  own tables/version bump later.
- The phase-1 sample's tables double as the phase-2 codegen regression
  fixture, so the sample must be kept in lockstep with the contract —
  enforced byte for byte by `tests_py/test_generate.py`.
- The dev prototype required one manual re-commissioning after the
  topology change; no other user-facing devices existed yet, so this
  cost was paid once and does not recur.
- Related standing decisions: ADR 0006 (upstream CHIP as the SDK whose
  types this contract shields the app from) and ADR 0009 (the
  Matter-explicit YAML schema this contract is the compile target of).
