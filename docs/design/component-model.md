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
| Channel type registry (canonical types/units) | Fixed with first component batch |
| Actuator channels (write-path semantics) | With first actuator components (`on_off`) |
| Out-of-tree component packaging | Own design doc after v0.1 (§8) |
| Component versioning vs core version | Coupled until out-of-tree exists |
