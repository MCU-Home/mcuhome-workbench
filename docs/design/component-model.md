# MCUHome Component Model — Design

> **Status: approved by the product owner (2026-08-03).**
> Builds on the approved YAML schema ([yaml-schema.md](yaml-schema.md))
> and builder pipeline ([builder-pipeline.md](builder-pipeline.md)).

## 1. What a component is

A component is the unit that connects one YAML concept to runtime
behavior: it declares *config schema* (Python, consumed by the builder)
and implements *behavior* (C, compiled into the firmware when — and only
when — a device uses it). Schema and implementation live side by side in
one folder and version in lockstep (the ESPHome lesson the scaffold
already committed to).

## 2. Component kinds

Following the schema's three wiring points:

| Kind | Selected by | Examples | Role |
|---|---|---|---|
| **peripheral** | `hardware.peripherals[].driver` | `sht4x`, `bme680`, `gpio-output`, `voltage-divider` | Wraps a Zephyr driver; exposes typed **channels** |
| **cluster** | `node.endpoints[].clusters` | `temperature_measurement`, `on_off` | Implements Matter cluster behavior; binds to channels |
| **core** | `network:`, `device:`, `automations:` | `thread`, `matter`, `power`, `automation-engine` | Platform plumbing, always structural |

The **channel** is the universal contract between kinds: a typed value
stream (`float`, unit, quality/timestamp) that peripherals produce,
clusters publish, and the automation engine reads/writes. Channel types
are checked at validation time (a `temperature_measurement` cluster
refuses a channel that isn't `temperature/°C`).

This richer model is the YAML-level design target; the current runtime
contract the builder will map onto is narrower and already implemented
— see `include/mcuhome/channel.h` and §9 below.

## 3. Component folder layout

```
components/<name>/
├── component.py        # declarative manifest: schema + build contribution
├── Kconfig             # MCUHOME_COMPONENT_<NAME> + component options
├── CMakeLists.txt      # sources, guarded by the Kconfig symbol
├── src/                # C implementation
├── dts/bindings/       # only if the component defines own DT bindings
│                       #   (e.g. mcuhome,gpio-output)
└── tests/              # twister suite + golden-config cases
```

## 4. The Python side: `component.py` is a manifest, not a program

Declarative-first: a component *declares* as data — validation schema,
Kconfig symbols to enable, devicetree node template, table entries to
emit. Arbitrary codegen logic in components is deliberately restricted
(ESPHome components interleave schema and C++ emission in free-form
Python; that power made their codegen hard to reason about). Sketch:

```python
component = Peripheral(
    name="sht4x",
    driver="sensirion,sht4x",          # compatible string (schema §5)
    bus=I2C(default_address=0x44),
    channels={
        "temperature": Channel(type="temperature", unit="°C"),
        "humidity": Channel(type="humidity", unit="%RH"),
    },
    kconfig=["SENSOR", "SHT4X", "MCUHOME_COMPONENT_SHT4X"],
    dts=I2CDeviceNode(),               # standard template; no custom code
)
```

Escape hatch for genuinely special cases: overridable hook methods —
but a component that needs none (the common case) is pure data,
trivially testable and lintable.

## 5. The C side: Zephyr-native contracts

- **Peripherals are Zephyr devices.** Generated devicetree nodes +
  Zephyr's own driver model do the heavy lifting; the peripheral
  component only adapts driver readings to MCUHome channels
  (`sensor_channel_get()` → channel table entry). Many peripherals need
  *zero own C code* — the generic sensor adapter covers every chip whose
  Zephyr driver speaks the standard sensor API.
- **Clusters implement a fixed vtable** (init / attribute read / write /
  command / tick) against the runtime's data-model layer, registered via
  Zephyr iterable sections — the generated `mcuhome_config.c` tables
  reference them by symbol, the linker keeps only what's used (builder
  principle §1).
- **No heap after init, ISR-safe boundaries, SED power budget** — the
  scaffold's embedded rules (AGENTS.md) are the component contract, and
  the `zephyr-code-reviewer` agent enforces them in review.

## 6. Worked example (end to end)

`main.yaml` says `driver: sensirion,sht4x` → builder resolves the
`sht4x` peripheral component → emits: DT node under the chosen I2C bus,
`CONFIG_SHT4X=y` + `CONFIG_MCUHOME_COMPONENT_SHT4X=y`, channel table
entries (`sht4x/0: temperature, humidity`). The `temperature_measurement`
cluster component binds channel → Matter attribute + report policy from
the YAML. At runtime: generic sensor adapter polls per `sampling:`,
writes the channel, cluster publishes per `report:`; the automation
engine sees the same channel. Three table rows, no generated logic.

## 7. Testing per component

- Golden-config cases: minimal YAML using the component → expected
  DT/Kconfig/table output (fast, no Zephyr).
- Behavior tests on `native_sim` with Zephyr's bus/sensor emulators
  (twister) — real driver path, no hardware.
- A component without tests does not merge (CI gate once CI exists).

## 8. Out-of-tree components (reserved, not v0.1)

The external-components escape valve (load components from a git repo,
a config-tree folder or a device-local folder) is a **first-class
goal** — it is what keeps the core repo's review load sane (ESPHome's
proven pressure-release valve, PO-confirmed as a model to follow).

Component **resolution order** (most specific wins, fixed now so the
config tree never has to change):

1. `devices/<name>/components/` — device-local
2. `<config-root>/components/` — shared across the whole config tree
3. git-referenced components — later: declared in the config with a
   repo URL **pinned to a tag/commit** (unpinned refs at most warn-and-
   build never silently float — reproducibility rule from the builder
   design applies to external code too)
4. built-in components (mcuhome core)

A name collision resolves to the more specific source and is reported
in the build summary — never a silent surprise. Design doc for the git
mechanism follows once the in-tree contract above has proven itself in
v0.1 — the contract is deliberately kept free of in-tree assumptions
(no relative includes, manifest is self-contained).

## 9. Open points

| Topic | Status |
|---|---|
| Channel type registry (canonical types/units) | Contract v1 implemented — see note below |
| Actuator channels (write-path semantics) | With first actuator components (`on_off`) |
| Out-of-tree component packaging | Own design doc after v0.1 (§8) |
| Component versioning vs core version | Coupled until out-of-tree exists |

**Channel contract v1 note:** `include/mcuhome/channel.h` implements a
deliberately narrow first cut, hardware-verified in phase 1: integer-only
values, expressed in the target Matter attribute's raw unit (no separate
float/unit/quality/timestamp fields), with a direct `(endpoint, cluster,
attr)` binding. The richer model of §2 remains the design target for the
YAML-level abstraction; the builder will map it onto this v1 contract,
with scale/offset conversion happening in the sensor binding, not the
channel itself. Any richer channel fields land as a contract-version
bump per ADR 0014.

## 10. Future direction: filters, scripting, and the DEV/LIVE split

Product-owner direction, 2026-08-07. Not v0.x scope — recorded here so
nothing built before the automation phase closes a door on it. The
formal decision is an ADR at the start of that phase, backed by a
measured prototype.

**Principle.** Firmware stays individually generated and compiled per
device, exactly as today — drivers, bus wiring, devicetree and the
Matter tables are compile-time (Zephyr instantiates drivers from
devicetree at build time; there is no runtime bus/address binding, and
MCUHome will not maintain a parallel driver stack to get one). A
scripting engine is added strictly as the *filter/hook/automation*
layer: it can transform values on their way from a sensor binding to a
channel and it can react to events, but it never becomes the data path
itself and never carries the device's Matter model. End users never
write C/C++.

**Three filter tiers**, escalating in capability and cost; the builder
picks the cheapest tier that covers the configuration:

1. **Predefined filters** (`offset`, `range`, later moving average,
   deadband, …) — declarative registry entries. Everything *stateful*
   lives here, with its state owned by the C framework.
2. **Expressions** — deliberately more than arithmetic (PO scope call,
   2026-08-07: end users should normally not need tier 3): variables,
   the ternary conditional, null coalescing (pairs naturally with the
   nullable "sensor not ready yet" semantics of the attribute stores),
   and read-only value access to other channels through a fixed method
   surface — e.g.
   `humidity_kitchen.value() > 30 ? temp_kitchen.value()
   : temp_kitchen.value() * 0.7 + temp_living.value() * 0.3`.
   Symfony's ExpressionLanguage marks the intended scope. The hard
   line stays: an expression is a single side-effect-free value — no
   statements, no loops, no user-defined functions, no state — so
   evaluation needs no heap and no GC, and cross-channel references
   form a static dependency graph the builder validates (recompute
   order, cycles rejected at validate time). This tier is small
   enough that MCUHome owns its implementation.
3. **Scripting engine** — stateful logic, `on_boot`-style hooks,
   timers, actions (e.g. `trigger_measurement()`), user automations.
   For genuinely complex processing (an AMG8833 8×8 thermal grid,
   say) the engine's footprint is a fair price — though known-complex
   sensors can also land as C components (§2), shrinking how often
   tier 3 is needed at all. Two candidate tracks, decided by the
   automation-phase ADR:
   - **Adopt: Berry** (MIT, MCU-native, Tasmota precedent); second
     choice Lua. Evaluated and behind: Toit (LGPL VM, ESP-IDF-bound),
     Wren (dormant since 0.4.0, double-precision-only numbers on
     single-precision-FPU targets).
   - **Grow our own** from the tier-2 core (PO wish, to be evaluated
     honestly): one language whose grammar's expression subset IS
     tier 2; host-side compilation to a compact bytecode (the builder
     is always in the loop, unlike Tasmota's on-device console — a
     device-side parser buys us nothing), so the MCU carries only a
     bytecode VM; the VM assembled from feature modules (arithmetic,
     functions, classes, …) so an expression-only device links an
     expression-only VM; and the language kept statically analyzable
     enough that the LIVE mode can transpile scripts to C instead of
     shipping the VM. Each element is individually proven prior art
     (Lua `luac` / MicroPython `.mpy` / Berry solidification;
     trimmed-library builds; DSL-to-C transpilers) — the risk is not
     buildability but a decade of ownership: first-class diagnostics,
     documentation, a bytecode verifier (pushed bytecode must never
     crash a node), and format stability across firmware versions.
     Berry remains the safety net if this track stalls.
     **If this track is chosen, the engine is a fully standalone
     project** (PO decision, 2026-08-07): own repository, a cleanly
     versioned C API toward MCUHome, and MCUHome pinning a concrete
     engine release exactly as it pins Zephyr and the Matter SDK —
     the engine evolves independently, MCUHome follows deliberately
     once a release is proven. Generally useful to other projects by
     design; license chosen at project creation (Apache-2.0 vs MIT —
     adoption argument, deliberately left open). The tier-2 expression
     engine is built with this API discipline from day one, so it can
     be promoted into the standalone project rather than rewritten.

**DEV/LIVE split.** A freshly set-up device runs in DEV mode: YAML
filters are lowered to *script* and pushed without recompiling —
config iteration lands in seconds. Once tuned, the user switches to
LIVE: one full rebuild bakes the YAML-defined filters back into C, and
the engine is linked only for what genuinely needs it (hand-written
hooks/automations) — possibly not at all, which is the steady state
for battery devices. Both lowerings of a filter primitive come from
one registry definition and are held equivalent by golden tests (same
input series, identical output). The builder classifies every config
diff as firmware-affecting (wiring, drivers, endpoint structure →
rebuild + OTA) or script-only (filters, automations → push), and the
device's mode is part of the canonical model so a filter is never
applied twice (baked *and* scripted).

**Fixed constraints for that phase:** the tables contract (ADR 0014)
stays the single interface — a boot script would be a second *producer*
of the same tables, never a bypass; unit conversion into Matter raw
units stays in the C binding (scripts work in user units); script
transport needs an authenticated channel (the CoAP management path)
plus staged apply with fallback to the last good script — a broken
script degrades to the identity path and logs, it never takes the node
down; and a script/firmware binding-API version handshake mirrors
`tables_version`. Real OTA remains required regardless, for base-image
security updates.
