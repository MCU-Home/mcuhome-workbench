# MCUHome Builder Pipeline — Design

> **Status: approved by the product owner (2026-08-03).**
> Builds on the approved YAML schema design ([yaml-schema.md](yaml-schema.md)),
> ADR 0007 (containerized toolchain) and ADR 0010 (Matter-only).
> Incorporates the PO requirements: device-as-folder config tree,
> shared-fragments folder, dashboard/build-server decoupling.
>
> ---
>
> **STATUS NOTE (2026-08-09) — §5 and §6 no longer describe the design,
> and §7's manifest is renamed and moves into the build container.**
>
> **[§5](#5-build-execution-per-adr-0007) and
> [§6](#6-build-service-boundary--local-and-remote) are superseded** by
> ADR 0017-0020 and
> [build-container-contract.md](build-container-contract.md). They are
> kept for the record, not as design; **everything else in this document
> stays valid**, except the two statements about
> [§7](#7-artifacts)'s manifest marked at the end of this note. Exactly
> what no longer holds:
>
> - **§6's stateless build requests — "No shared filesystem, no
>   server-side session state".** The unit of interaction is a
>   **session**: one session = one build environment = one effective
>   context, with state surviving from one command to the next
>   (ADR 0019 decisions 1-2). The session's persistent working area is a
>   named path the backend supplies on every invocation (contract §4,
>   `work`).
> - **§6's "thin HTTP wrapper" build server.** The client interface is
>   the session verb set of ADR 0019 decision 2 over WebSocket with a
>   bearer token (ADR 0019 decision 1; the transport itself carries
>   forward from dashboard ADR 0006 decisions 1-2). Towards the build
>   environment the interface is the frozen invocation ABI of the
>   build-container contract (ADR 0019 decision 4, contract §5), which
>   is what lets a build server drive **any** conforming build
>   container, not only ours.
> - **§5's "the add-on container *is* the builder image plus
>   dashboard".** The Home Assistant case is the `subprocess` backend
>   profile: the build environment runs in the same filesystem as the
>   build server, but as a separate process, with the same ABI and the
>   reduced guarantees named there (contract §1.2). What is shared is
>   the filesystem, not the process — a build server orchestrates and is
>   never itself the build environment, in either profile. The dashboard
>   carries no toolchain (ADR 0017 §2) and never compiles (dashboard
>   ADR 0003 decision 2, the part of that ADR that carries forward).
> - **§5's persistent volume carrying ccache plus the west workspace
>   across runs.** The west workspace is baked into the build-container
>   image, and the program assembles its build environment from the
>   trees it is handed (contract §6.1). ccache is an optional path in
>   the request document whose writability the backend asserts rather
>   than the program probing it — read-only secondary storage for
>   untrusted work, writable only for an operator's own cache warming
>   (contract §10, §4.1; ADR 0019 decision 6).
>
> Nothing else in §5 or §6 is superseded by this note — in particular
> not §5's ccache requirement and its GN compiler-launcher finding, and
> not §6's rule that the canonical device model is the wire format,
> which ADR 0018 decision 1 keeps by putting `device-model.json` inside
> the build context.
>
> **[§7](#7-artifacts) stays, with two exceptions — the manifest's name,
> and where it is written.** What a build produces is unchanged: the
> artifact set of §7's table stays, and so does the memory report. What
> no longer holds as written:
>
> - **The document is called `build-report.json`.** The build-side
>   report is `build-report.json`, artifact role `report`, and it
>   carries the `signing` block §7 describes — the `imgtool` parameters
>   the client needs for detached signing (contract §7.2, §5.4;
>   dashboard ADR 0007 decision 3). The block's content is the one §7
>   states; only the document it lives in is renamed.
> - **It is written inside the build container**, by the program, and
>   declared in the result document's `artifacts` list, which is
>   mandatory for a successful `build` (contract §5.4, §7.2). Producing
>   it belongs to stage 5 and therefore to `mcuhome-compiler`, which
>   runs inside the build container (ADR 0020 decision 1) — §7's
>   "implemented (`mcuhome/model/manifest.py`)" describes that document's
>   implementation, not a host-side step after the build.
>
> The contract governs the names and roles under which artifacts leave
> the build container — `firmware.hex`/`firmware.bin` (role `firmware`)
> and `build-report.json` (role `report`), with **no `ota` role in v1**
> (contract §7.2, §5.4). That leaves §7's `.ota` file where §7 already
> puts it in a detached build: written by `mcuhome sign`, on the machine
> holding the private key, because the wrapper's payload has to be the
> signed image and the program in the container must not sign.
>
> Terminology: "builder container" and "builder image" below read as
> **build container** and build-container image; "the lib" reads as the
> packages of ADR 0020 decision 1.

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

> **Superseded in part — see the status note at the top of this
> document.**

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
- `--method local-dev` escape hatch: developers with a local west workspace
  (contributors to MCUHome itself) can run stage 5 on the host. CI and
  end users always use the container.

## 6. Build service boundary — local and remote

> **Superseded in part — see the status note at the top of this
> document.**

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
  resolved model, the response carries `build-manifest.json` + artifacts;
  both ends speak device-model JSON, nothing else. **Implemented on the
  builder side**: `mcuhome build --model <device-model.json>` starts at
  stage 4, and reads no configuration tree and no secrets file at all
  (dashboard ADR 0007 decision 4) — a build server has no business
  holding either. `mcuhome.api.read_model` is the same thing in process,
  and it refuses a `model_version` it does not implement by naming both
  numbers rather than guessing. The two routes produce byte-identical
  trees, which is what makes the split a contract instead of a hope; the
  file name of the source configuration is a field of the model
  (`device.source`) for exactly that reason, since the generated headers
  name it and stage 4 may read nothing but the model.
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
| `<device>-<version>.ota` | application | Matter OTA image (header + the signed payload above) |
| `build-manifest.json` | — | device model + versions + image hashes — consumed by the dashboard |
| `memory-report.txt` | per image | ROM/RAM footprint — regression tracking |

**`build-manifest.json` is implemented** (`mcuhome/model/manifest.py`). It sits
at the top of the build directory next to `device-model.json`, and every
path in it is relative to that directory, because a manifest crosses a
network (§6). It carries the device name, board and model version, the
builder's version, the snippets and job count the build ran with, one
entry per image (role, files, size, SHA-256 per file), the combined hex,
an `ota` block (below), and a `signing` block: the `imgtool` arguments — `--version`,
`--header-size`, `--slot-size`, `--align` — under imgtool's own option
names, the input and output artifact of each format, and two booleans,
`signed_by_the_build` (how the build ran, never changes) and `signed`
(whether a signature exists in the directory now). Three of the four
signing arguments come from the board's registry entry, which is the same
partition table stage 4 rendered into the overlay; the fourth,
`--version`, is read out of the built application's Kconfig. The document
is deterministic apart from the sizes and hashes it measures: no
timestamps, no host names, no absolute paths.

**The Matter OTA file is implemented** (`mcuhome/model/ota.py`, ADR 0015
decision 5). It is written for a device that can actually receive one —
the board's update scheme has a staging slot and the device has a Matter
stack — and it wraps the **signed** application image, so an inline build
writes it at the end and a detached build only gets it from `mcuhome
sign`. The manifest's `ota` block exists in both cases and carries the
version, the Matter `SoftwareVersion` derived from it (ADR 0015 decision
9), and the vendor and product IDs; `path`/`size`/`sha256` are null until
the file exists. That is what lets the machine holding the signing key
produce the .ota without a device configuration and without the Matter
SDK: MCUHome writes the format itself rather than calling CHIP's
`ota_image_tool.py`, and the pytest suite compares the two byte for byte
wherever the SDK is present.

`memory-report.txt` is still to come; the memory figures are reported to
the terminal today and carried per image in the manifest as
`flash_bytes`.

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

The command vocabulary, its flags and the `--json`/exit-code contract
are the CLI's own decisions, recorded in the cli repository since
2026-08-14 (vocabulary: cli ADR 0003; output/exit-code contract: cli
ADR 0004; configuration and builder selection are platform decisions,
ADR 0022/0023). The enumeration this section used to carry had drifted —
it listed `--keep-going`, which was never built — and is not repeated
here.

What stays pipeline-relevant: `<device>` is a folder name resolved
against the config tree root (`devices/<name>/main.yaml`; an explicit
path works too; tree root `--config-root`, else auto-discovered cwd
upwards — as built today; the target model, a project directory with
`mcuhome.yaml` and `--project-dir`, is ADR 0022). `init-pairing` is the exception to "the builder never
writes into the configuration tree" (§2), and it exists because of
§1.4: a device needs credentials nobody else has, and a build has to
be reproducible, so the randomness happens once — into the device's
own YAML or the tree's `secrets.yaml` — and every build after that is
deterministic input in, deterministic bytes out (yaml-schema.md §4.1).
`new` is the other end of the same rule: it deliberately draws no
credentials, so re-running it after a mistake cannot silently
invalidate every controller that already knows the device. Everything
else (`flash`, `logs`, `migrate`, `update`) arrives with its own
design. In process, the supported programmatic surface is
`mcuhome.workbench.api`.

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
| device-model.json schema versioning | **Closed.** `MODEL_VERSION` is 1 and is a published contract: the dashboard pins what it sends and what it can read (`versions.py`); the server-side range advertisement retired with the job protocol (dashboard ADR 0007 decision 4) |
| Builder image layout/registry | Decided with the first image (`containers/builder/`): Debian 13 base, tools only, `ghcr.io/mcu-home/builder:zephyr-<line>-r<rev>` |
| `mcuhome migrate` (ESPHome import) | Later milestone (ADR 0009) |
