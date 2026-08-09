# components/matter/zap/

The **single framework-owned ZAP configuration** of MCUHome, plus the
`.matter` IDL that ZAP derives from it.

| File | Role |
|---|---|
| `mcuhome-root.zap` | ZAP data-model configuration: **endpoint 0 (root node) only** |
| `mcuhome-root.matter` | Generated IDL view of the same configuration; consumed by CHIP's `cpp-app` codegen (`chip_configure_data_model()` infers it from the `.zap` name) |

## What this describes

Endpoint 0 — the Matter root node — with the mandatory root clusters
(Basic Information, General Commissioning, Network Commissioning,
Operational Credentials, Access Control, Descriptor, Diagnostics, …).
Nothing else. The generated `endpoint_config.h` therefore has
`FIXED_ENDPOINT_COUNT (1)` and a single device type entry
`{{0x0016, 3}}` (Root Node, revision 3).

Derived from CHIP v1.5.1.0's `examples/bridge-app/bridge-common/bridge-app.zap`
by deleting its endpoints 1 (Aggregator) and 2 (Dimmable Light).

## Hard rule: endpoints other than 0 are forbidden here

MCUHome devices are **native composed nodes, not bridges** (ADR 0014).
Every application endpoint is registered at runtime through
`emberAfSetDynamicEndpoint()` from tables the builder generates out of the
user's YAML — device endpoints start at EP1, directly under the root
(parent endpoint 0), with no aggregator in between.

A statically compiled endpoint in this file would occupy an endpoint ID
that the dynamic-endpoint allocator also hands out, and would appear to
controllers as a ghost device the YAML never asked for. So: **never add an
endpoint here.** The rule is enforced by a compile-time assert on
`FIXED_ENDPOINT_COUNT` in `components/matter/src/matter_init.cpp`.

## Regeneration

These files are **not** regenerated per device — a device's data model is
runtime state, not build state. They are touched only on MCUHome release
events:

- a CHIP version bump (`west.yml`) whose ZCL definitions changed, or
- a deliberate change to the root-node cluster set.

Regeneration means editing `mcuhome-root.zap` in the ZAP GUI
(`zap mcuhome-root.zap`) and saving, then refreshing the `.matter` IDL and
re-checking the generated C:

The commands below need `zap`/`zap-cli` and CHIP's codegen dependencies.
The builder image provides all of them, so the simplest way to run them
is inside it — from the workspace top directory:

```sh
docker run --rm -it --user "$(id -u):$(id -g)" \
    --volume "$PWD:$PWD" --workdir "$PWD" \
    ghcr.io/mcu-home/builder:zephyr-4.4.0-r5 bash
```

(The GUI step needs a real `zap` on a desktop, so that one stays a host
task — the image carries `zap-cli` and the headless codegen it drives,
not a working `zap` window. Everything below is headless.)

```sh
# From the workspace top directory. Prerequisites, if you are NOT in the
# builder image:
#   - zap / zap-cli on PATH (or ZAP_INSTALL_PATH pointing at the zap install)
#   - CHIP's codegen dependencies (scripts/setup/requirements.build.txt)
# Needed either way:
#   - PYTHONPATH=mcuhome/scripts/pyshim   (see scripts/pyshim/README.md)
CHIP=$PWD/modules/lib/connectedhomeip
export PYTHONPATH=$PWD/mcuhome/scripts/pyshim
mkdir -p /tmp/zapcheck

# 1. Refresh mcuhome-root.matter (written into the scratch dir as
#    Clusters.matter, then moved next to the .zap by generate.py)
python3 $CHIP/scripts/tools/zap/generate.py \
    $PWD/mcuhome/components/matter/zap/mcuhome-root.zap \
    --templates $CHIP/src/app/zap-templates/matter-idl-server.json \
    --zcl       $CHIP/src/app/zap-templates/zcl/zcl.json \
    --no-prettify-output -o /tmp/zapcheck

# 2. Verify the C generation the build performs
python3 $CHIP/scripts/tools/zap/generate.py \
    $PWD/mcuhome/components/matter/zap/mcuhome-root.zap \
    --templates $CHIP/src/app/zap-templates/app-templates.json \
    --zcl       $CHIP/src/app/zap-templates/zcl/zcl.json \
    --no-prettify-output -o /tmp/zapcheck
grep -E 'FIXED_ENDPOINT_COUNT|FIXED_DEVICE_TYPES' /tmp/zapcheck/endpoint_config.h
```

The output directory must already exist and paths are best given
absolutely — `generate.py` resolves relative arguments against the CHIP
root, not the current directory.

Passing `--zcl` explicitly is required: the path stored inside the `.zap`
is relative to CHIP's own example directories and does not resolve from
here. The build passes the same path via `chip_configure_data_model(...
ZCL_PATH ...)` for exactly this reason.

Expected output of the last command:

```
#define FIXED_ENDPOINT_COUNT (1)
#define FIXED_DEVICE_TYPES {{0x00000016,3}}
```

Known, accepted generator warning: *"On endpoint 0, cluster: Diagnostic
Logs server, outgoing command: RetrieveLogsResponse should be enabled…"* —
inherited unchanged from the upstream bridge-app configuration, unrelated
to the endpoint trim.
