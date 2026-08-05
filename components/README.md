# components/

MCUHome components live here. Each component pairs its Python configuration
schema/codegen with its C sources side by side (the ESPHome-proven layout),
wired into the module's Kconfig and CMake trees.

The Python side does not exist yet — the `component.py` manifests
([`docs/design/component-model.md`](../docs/design/component-model.md) §4)
arrive with builder phase 2. What is here today is the C side.

| Component | Kind | Role |
|---|---|---|
| `matter/` | core | The Matter runtime: bring-up sequence, generated-tables → ember translation, external attribute callbacks, DNS-SD retry service (ADR 0014). The only place in the tree that includes a CHIP header. |
| `sensor/` | peripheral | The generic Zephyr-sensor channel adapter: one workqueue-driven poller that samples a static binding array, converts each reading into its Matter attribute's raw unit and publishes on change. This is the runtime that lets a sensor chip with a stock Zephyr driver become an endpoint with no C code of its own. |
