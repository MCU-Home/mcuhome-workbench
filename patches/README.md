# Workspace patches (Matter integration prototype)

Patches applied on top of the pinned west projects to make upstream
Matter (connectedhomeip v1.5.1.0) build and run on vanilla Zephyr
v4.4.0. Background, root-cause analysis and per-patch rationale:
[docs/design/matter-zephyr-integration.md](../docs/design/matter-zephyr-integration.md).

| File | Applies to | Content |
|---|---|---|
| `connectedhomeip-v1.5.1.0-vanilla-zephyr.patch` | `modules/lib/connectedhomeip` | PSA/NCS-symbol guard fixes, mbedTLS-4 cert-code port, chip-module flag forwarding, Zephyr 4.x BLE macro, chip_crypto=psa default, `build_overrides/pigweed_environment.gni` stub (new file), `chip_gn.cmake` GN/ninja job cap, deprecated `FIXED_PARTITION_*` macros in the Zephyr OTA image processor |
| `zephyr-v4.4.0-nrf53-spinel-stack.patch` | `zephyr` | Overridable spinel send-thread stack size (hardcoded 1 KB upstream) |

Today these are applied by hand: `git apply <patch>` inside the
respective west project. Automatic application is **specified** — the
build-container contract makes each patched west project a *patched
layer* whose patches the build program applies to a writable view of the
tree, once per session, before building
([docs/design/build-container-contract.md](../docs/design/build-container-contract.md),
§6.2 "Patched layers: writable views, applied once"). That is a
specification, not working code: nothing applies these patches
automatically yet, so a fresh workspace still needs the manual step
above. Several hunks are upstream-issue candidates (tracked outside the
repo for now); only what upstream does not take stays here.

The mbedTLS legacy-header shims the chip-module hunk puts on CHIP's
include path are **not** in this directory and are not generated: they
are ordinary files in this repository, at [compat/mbedtls/](../compat/),
reached as `${ZEPHYR_MCUHOME_MODULE_DIR}/compat` so that they travel with
the SDK instead of with whoever's workstation. They are not diff output,
which is the only reason they do not live here; see
[compat/README.md](../compat/README.md) for what they are and when they
can be deleted.

To change a hunk: edit the already-patched file directly in the west
project checkout (it is a normal git repo pinned to the upstream tag,
with these patches applied as uncommitted working-tree changes — `git
status`/`git diff` there shows exactly the patch set), then regenerate
the patch file with `git diff > patches/<name>.patch` from inside that
checkout (one exception, below). Never hand-edit hunk headers or line
offsets — a diff and the
tree it came from drift apart the moment one is edited without the
other. Verify with `git apply --check <patch>` against a clean worktree
of the pinned tag (`git worktree add <path> <tag>`) before trusting it —
not against the working checkout, where the patch is already applied.

One hunk needs care when regenerating: `build_overrides/pigweed_environment.gni`
is a *new* file and CHIP's own `.gitignore` lists it, so `git status` and
`git diff` never show it and a plain `git diff > patches/<name>.patch`
**silently drops that hunk**. Regenerate with the file force-staged:

```sh
git add -f build_overrides/pigweed_environment.gni
git diff HEAD > <workspace>/mcuhome/patches/connectedhomeip-v1.5.1.0-vanilla-zephyr.patch
git restore --staged build_overrides/pigweed_environment.gni
```

`src/platform/Zephyr/OTAImageProcessorImpl.cpp` uses
`FIXED_PARTITION_OFFSET`/`FIXED_PARTITION_SIZE`, which Zephyr 4.4 marks
`__DEPRECATED_MACRO` while CHIP's GN build compiles with `-Werror` — so on
our pin `CONFIG_CHIP_OTA_REQUESTOR=y` does not warn, it fails to build
(workspace `UPSTREAM-BUGS.md` entry C10). The hunk drops the `FIXED_`
prefix, which is the spelling Zephyr 4.4 wants. It is needed even though
MCUHome does not *use* that file — `components/matter/src/ota_image_processor.cpp`
replaces its behaviour, because upstream's hardcodes the internal flash
controller and MCUHome's staging slot is on an external part (ADR 0015
decision 3) — because CHIP's `BUILD.gn` compiles it whenever the requestor
is enabled, regardless of what the application instantiates.

`config/common/cmake/chip_gn.cmake`'s `chip-gn` `ExternalProject_Add`
calls a bare `ninja` for CHIP's own GN sub-build, so upstream it always
runs at ninja's default parallelism (nproc+2) no matter what job count
the outer west/ninja build was given — an OOM risk on RAM-constrained
targets (the Home-Assistant-add-on class hardware ADR 0007 targets). The
patch adds a job cap read from the `MCUHOME_CHIP_JOBS` environment
variable at CMake configure time; unset or empty keeps the upstream
default. The builder sets it to the same value as its own `-o=-jN` job
cap — auto-detected from CPU count and available RAM, `--jobs`/
`MCUHOME_JOBS` override it (`mcuhome.workspace.resolve_jobs`,
`mcuhome.workspace.auto_jobs`) — in both the native environment
(`mcuhome/workspace.py:build_environment`) and the container
environment (`mcuhome/container.py:container_environment`).

CHIP codegen also needs a build prerequisite outside these patches: its
release tarball is missing the `python_path` helper its codegen scripts
import (upstream candidate C1). The stand-in is checked in at
[scripts/pyshim/](../scripts/pyshim/) — export
`PYTHONPATH=scripts/pyshim` before running CHIP codegen or building
Matter apps; see `scripts/pyshim/README.md`.
