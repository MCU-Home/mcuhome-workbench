# MCUHome Builder Pipeline — Design

> **Status: approved by the product owner (2026-08-03).**
> Builds on the approved YAML schema design ([yaml-schema.md](yaml-schema.md)),
> ADR 0007 (containerized toolchain) and ADR 0010 (Matter-only).
> Incorporates the PO requirements: device-as-folder config tree,
> shared-fragments folder, dashboard/build-server decoupling.

## 1. Principles

1. **Thin code generation.** The builder generates *data*, not logic:
   static configuration tables, a devicetree overlay and Kconfig
   fragments. All behavior lives in the generic MCUHome runtime
   (C, in `components/` and `lib/`) which interprets those tables.
   ESPHome generates C++ program logic per config — we deliberately
   don't: generated logic is hard to test, diff and debug.
   *Size note:* this does **not** bloat binaries — component selection
   still happens at compile time (Kconfig enables only what the YAML
   uses; the linker strips the rest). The interpreter engine costs a few
   KB; the flash budget is dominated by the Matter/Thread stacks
   (~600–800 KB), which is also what defines the minimum viable MCU.
   The per-build memory report (§7) tracks this permanently.
2. **One canonical intermediate model.** Validation and resolution
   produce a normalized "device model" (JSON): the single internal
   representation between YAML and generators, the transfer format for
   remote builds (§6), and the contract the dashboard consumes
   (schema-versioned). No generator reads raw YAML.
3. **Every intermediate artifact is inspectable.** The build directory
   contains the resolved model and all generated files as plain text —
   debuggable with standard tools, no hidden state.
4. **Reproducible by construction.** Pinned west workspace (ADR 0008),
   versioned builder container (ADR 0007): same config tree + same
   MCUHome version = same image, on any machine — including someone
   else's build server.
5. **Fail early, fail precisely.** The validation layers from the schema
   design run before anything is generated; every error carries
   file/line/key and a fix hint.

## 2. Configuration tree

A device is always a **folder**, never a bare file; reusable fragments
live in a parallel folder. Layout (Home Assistant add-on example —
standalone use has the same tree under any root):

```
/config/mcuhome/
├── devices/
│   ├── bedroom-climate/
│   │   └── main.yaml            # entry point of this device
│   └── office-co2-guard/
│       ├── main.yaml
│       └── notes.md             # device-local files are fine
├── shared/
│   ├── thread-sed-defaults.yaml # reusable fragments (consumed by the
│   └── i2c-standard-pins.yaml   #  packages mechanism, schema rev. 2)
├── components/                  # tree-wide custom components (see the
│                                #  component-model design, §8)
└── secrets.yaml                 # one secrets store for the whole tree
```

- `devices/<name>/main.yaml` is the canonical entry point; the folder
  name is authoritative for tooling (`mcuhome build bedroom-climate`).
  Device folders later also host device-local custom components and
  extra fragments — the folder-per-device rule makes that growth free.
- `shared/` is reserved for reusable fragments now and becomes active
  together with the packages/include mechanism (schema revision 2);
  creating the folder and its semantics from day one avoids a migration.
- `components/` holds custom components shared across the tree;
  device-local ones live in `devices/<name>/components/`. Resolution
  order and the future git-referenced mechanism are defined in the
  component-model design (§8).
- `secrets.yaml` lives at tree root (`!secret` resolves against it).
- Build output does **not** pollute the config tree: it goes to a
  separate work dir (`build/<device>/`, location configurable) — the
  config tree stays clean, diffable and git-friendly for users.

## 3. Pipeline stages

```
devices/<name>/main.yaml
  │  1 load        YAML parse, !secret resolution
  │  2 validate    schema shape → cross-refs → board capabilities → Matter conformance
  │  3 resolve     defaults, device-type completion, endpoint 0 synthesis
  ▼                → device-model.json  (canonical model)
  │  4 generate    from the model only:
  │                  ├─ app/boards/<board>.overlay   (board wiring + flash layout + hardware:)
  │                  ├─ app/prj.conf fragments       (from network:/power:/components)
  │                  ├─ mcuhome_config.c/.h          (endpoint/cluster/automation tables)
  │                  ├─ app/CMakeLists.txt           (generated app skeleton)
  │                  ├─ app/sysbuild.conf            (bootloader, mode, signature type)
  │                  └─ app/sysbuild/mcuboot.{conf,overlay}   (the bootloader image)
  │  5 build       west build --sysbuild inside the builder container
  ▼
artifacts, per image: MCUboot + the signed application, plus the combined
hex, build-manifest.json and the memory report
```

The flash layout and the bootloader configuration are **per-board
registry data** (ADR 0015 decision 2), not generator logic: stage 4
renders `BoardDef.update_scheme` into the two devicetree overlays and the
two Kconfig fragments above, and nothing in the builder branches on a
board name.

- Stages are separately invocable (`mcuhome validate`, `mcuhome build`);
  stage 4's output is a complete, standalone Zephyr application that
  consumes the MCUHome Zephyr module — a developer can `west build` it
  manually without the builder.
- Automations compile to a compact static table (triggers, conditions,
  actions as data) interpreted by a small runtime engine — no generated
  C control flow.

## 4. Matter data model wiring (verified by prototype)

The Matter SDK's conventional path generates cluster code from ZAP files
at compile time. For a YAML-driven framework that is hostile: ZAP is a
heavy toolchain and static per-config codegen contradicts §1. The
integration prototype (2026-08-04, see
[matter-zephyr-integration.md](matter-zephyr-integration.md))
**verified dynamic endpoint registration at runtime on hardware**
(nRF5340, upstream CHIP v1.5.1.0): endpoints register from tables via
`emberAfSetDynamicEndpoint`, with a static ZAP-generated data model only
for the fixed root endpoint — generated once per MCUHome release, not
per device config. `CHIP_DEVICE_CONFIG_DYNAMIC_ENDPOINT_COUNT` sizing is
resolved (ADR 0014): it derives automatically from
`CONFIG_MCUHOME_MATTER_MAX_DYNAMIC_ENDPOINTS`
(`include/mcuhome/matter/chip_project_config.h`), so the builder's
remaining job there is at most a Kconfig passthrough. Requirements the
builder still owns: zap-cli/gn provisioning and the vanilla-Zephyr patch
set ([../../patches/](../../patches/)).

## 5. Build execution (per ADR 0007)

- `mcuhome build` runs stage 5 inside the versioned builder image
  (Zephyr SDK + pinned west workspace pre-baked). Host needs docker
  only; a persistent volume carries ccache + west workspace across runs.
- **ccache is a hard requirement of the builder image** (PO decision,
  2026-08-03), not an optimization: on Raspberry-class Home Assistant
  hosts without a remote build server it is the difference between
  usable and painful rebuild times. Prototype finding to honor: Zephyr's
  CMake side picks ccache up automatically, but the Matter SDK's inner
  GN build invokes the compiler directly — the builder must explicitly
  route it through ccache (compiler-launcher wiring in the GN args).
- Inside the Home Assistant add-on the same code path runs natively —
  the add-on container *is* the builder image plus dashboard.
- `--native` escape hatch: developers with a local west workspace
  (contributors to MCUHome itself) can run stage 5 on the host. CI and
  end users always use the container.

## 6. Build service boundary — local and remote

The dashboard never calls the builder directly; it talks to a **build
service interface** with two implementations:

| Implementation | v0.1 | How |
|---|---|---|
| **Local** | yes | same container, in-process invocation of the builder package |
| **Remote build server** | designed now, built later | HTTP API on a user-operated machine running the same builder image |

Design rules that make "remote" cheap later (and are therefore fixed
now, even though v0.1 only ships "local"):

- **Stateless build requests.** Input: a self-contained bundle (the
  device folder + referenced `shared/` fragments + resolved secrets +
  MCUHome version). Output: the artifact set from §7 plus logs. No
  shared filesystem, no server-side session state.
- **The canonical model is the wire format** — the request carries the
  config bundle, the response carries `build-manifest.json` + artifacts;
  both ends speak device-model JSON, nothing else.
- **Version negotiation.** A build server advertises the MCUHome/builder
  image versions it can build; the dashboard picks the match for the
  config. Mismatch is an error, never a silent fallback.
- **Same code everywhere.** The build server is a thin HTTP wrapper
  around the identical builder package/container — no second build
  implementation to maintain.
- Rationale: HA (and thus the dashboard) often runs on a Raspberry Pi;
  compiling Zephyr+Matter there is slow. Anyone can point the dashboard
  at a beefier machine running the build-server container.
- Open sub-topic for the build-server design doc: secrets transport
  (send-with-bundle vs. server-side injection) and authentication.

## 7. Artifacts

Since ADR 0015 a build produces **a set per image**, not one file. Under
sysbuild each image has its own sub-directory of the build tree
(`<build>/mcuboot/`, `<build>/<app>/`):

| Artifact | Image | Purpose |
|---|---|---|
| `zephyr.hex` / `.bin` / `.elf` | MCUboot | the bootloader, installed once per device by the ADR 0016 bootstrap |
| `zephyr.signed.hex` / `.bin` | application | what MCUboot chain-loads, and what an update carries |
| `merged.hex` | — | every image at its own offset, for a full-chip flash over a debug probe |
| `zephyr.uf2` | application | drag-and-drop bootstrap on UF2 boards (ADR 0016 decision 5) |
| `ota.bin` | application | Matter OTA image (header + the signed payload above) |
| `build-manifest.json` | — | device model + versions + image hashes — consumed by the dashboard |
| `memory-report.txt` | per image | ROM/RAM footprint — regression tracking |

**`build-manifest.json` is implemented** (`mcuhome/manifest.py`). It sits
at the top of the build directory next to `device-model.json`, and every
path in it is relative to that directory, because a manifest crosses a
network (§6). It carries the device name, board and model version, the
builder's version, the snippets and job count the build ran with, one
entry per image (role, files, size, SHA-256 per file), the combined hex,
and a `signing` block: the `imgtool` arguments — `--version`,
`--header-size`, `--slot-size`, `--align` — under imgtool's own option
names, the input and output artifact of each format, and two booleans,
`signed_by_the_build` (how the build ran, never changes) and `signed`
(whether a signature exists in the directory now). Three of the four
signing arguments come from the board's registry entry, which is the same
partition table stage 4 rendered into the overlay; the fourth,
`--version`, is read out of the built application's Kconfig. The document
is deterministic apart from the sizes and hashes it measures: no
timestamps, no host names, no absolute paths.

`ota.bin` and `memory-report.txt` are still to come; the memory figures
are reported to the terminal today and carried per image in the manifest
as `flash_bytes`.

The unsigned `zephyr.bin` is kept as well, and not only for the memory
report: signing is a detached `imgtool` step over the finished binary, so
a remote builder returns the unsigned image and the signature is applied
where the key is (ADR 0015 decision 8). **That path is implemented too**:
`mcuhome build --no-sign --public-key <file>` gives sysbuild the public
half of the key pair — enough for MCUboot, which compiles the public key
in, and useless for signing — and the generated tree's `sysbuild.cmake`
clears the application image's key setting, which makes Zephyr's
`cmake/mcuboot.cmake` skip signing entirely rather than write an unsigned
file with `signed` in its name. `mcuhome sign <build dir>` then runs
`imgtool` with the manifest's parameters, wherever the private key is.
Such a build deliberately leaves no `merged_*.hex` behind either:
sysbuild fills it with the *unsigned* application when there is no signed
one, which would be a file that looks flashable and bricks the boot.

Equivalence between the two paths is "byte-identical image, different
signature", and that is the strongest statement available: ECDSA draws a
fresh random nonce per signature, so two signings of the same bytes with
the same key differ in the signature TLV (occasionally in its length) and
in nothing else. `tests_py/test_imgtool.py` asserts exactly that —
header, payload, protected TLVs and the SHA-256 over all of them equal,
signature different, both verifying.

Measured once on the real toolchain (nRF7002-DK, the BMP180 example, one
build directory built both ways): the **bootloader is byte-identical**
whether sysbuild is given the private key or only its public half, and
the two signed applications agree in header (512 B), payload
(564,396 B), flags, protected TLVs, image digest and key hash, differing
only in the ECDSA signature — 71 bytes against 72, which is the DER
length of a random nonce.

Flashing UX (`mcuhome flash`, browser-based flashing from the dashboard)
is its own later design; the artifacts above are designed so both work.

## 8. CLI surface (v0.1)

```
mcuhome new          <device>      # scaffold a device folder from a board
mcuhome validate     <device>      # stages 1–3, prints resolved summary
mcuhome build        <device>      # stages 1–5
mcuhome sign         <build-dir>   # apply the signature detached (§7)
mcuhome init-pairing <device>      # draw commissioning credentials, once
mcuhome public-key                 # the public half of the signing key
mcuhome schema       [config|registry]   # the contract, as JSON
mcuhome clean        <device|--all>
```

`<device>` is a folder name resolved against the config tree root
(`devices/<name>/main.yaml`); an explicit path works too. Tree root:
`--config-root`, else auto-discovered (cwd upwards). Everything else
(`flash`, `logs`, `migrate`, `update`) arrives with its own design.
Flags: `--native` (§5), `--keep-going` for CI, `-v`.

`init-pairing` is the exception to "the builder never writes into the
configuration tree" (§2), and it exists because of §1.4: a device needs
credentials nobody else has, and a build has to be reproducible, so the
randomness happens once — in this command, into the device's own YAML or
the tree's `secrets.yaml` — and every build after that is deterministic
input in, deterministic bytes out (yaml-schema.md §4.1).
`validate`/`build` print the resulting pairing codes and store them
nowhere.

`new` is the other end of the same rule: it writes a device's first
`main.yaml` and deliberately does *not* draw its credentials, so that
re-running it after a mistake cannot silently invalidate every controller
that already knows the device. It prints `init-pairing` as the next step
instead.

`validate` and `build` take `--json`, which replaces the human rendering
with one machine-readable document on stdout — the canonical model or the
build manifest, and on failure the errors of `ConfigError.to_dict()`
(message, tree-relative file, line, column, dotted key, hint, kind). Exit
codes do not change; the build log moves to stderr. `mcuhome schema`
emits the two documents a consumer needs statically: a JSON Schema for
`main.yaml` and the registry as data. All of it is also reachable in
process through `mcuhome.api`, which is the supported programmatic
surface (dashboard ADR 0011 decision 1).

## 9. Testing strategy

- **Golden-file tests** for stages 1–4: example YAMLs → expected
  device-model.json / overlay / fragments / tables, byte-exact
  (pytest, runs without Zephyr — fast).
- **Compile tests**: golden outputs build against `native_sim` and one
  real board per release via twister — this is where repo CI starts
  (per the scaffold decision: CI lands together with the first tests).
- Validation error messages are tested (bad configs → expected
  error + location), because they are UX.

## 10. Open points

| Topic | Status |
|---|---|
| Dynamic endpoints vs ZAP fallback | Prototype first, then ADR (§4) |
| Build-server API details (auth, secrets transport) | Own design doc, pre-dashboard |
| Flashing UX (CLI + browser) | Own design doc |
| device-model.json schema versioning | **Closed.** `MODEL_VERSION` is 1 and is a published contract: the dashboard sends one version, a build server advertises the range it supports, a mismatch is a refusal (dashboard ADR 0007 decision 4) |
| Builder image layout/registry | Decided with the first image (`containers/builder/`): Debian 13 base, tools only, `ghcr.io/mcu-home/builder:zephyr-<line>-r<rev>` |
| `mcuhome migrate` (ESPHome import) | Later milestone (ADR 0009) |
