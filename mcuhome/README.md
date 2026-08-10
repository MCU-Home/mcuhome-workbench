# mcuhome/ — the Python source tree

The Python side of MCUHome: it reads a YAML device configuration and
produces Zephyr firmware. Design records:
[builder-pipeline.md](../docs/design/builder-pipeline.md),
[yaml-schema.md](../docs/design/yaml-schema.md),
[component-model.md](../docs/design/component-model.md).

```sh
# from the repository root: the three packages, editable, plus pytest
pip install -e ./packaging/model -e ./packaging/workbench \
            -e ./packaging/compiler 'pytest>=8.0'
pip install -e ../cli            # the `mcuhome` command (own repo, github.com/mcu-home/cli)
mcuhome init-pairing <device>    # draw this device's commissioning credentials
mcuhome validate <device>        # stages 1-3, prints the resolved device
mcuhome build <device> --generate-only   # + stage 4, writes the application
mcuhome build <device>           # + stage 5, compiles it in the builder image
mcuhome build <device> --method local-dev   # … or on this machine's own toolchain
pytest                           # the suite in ../tests_py/
```

## Three packages, one directory

This directory is a **PEP 420 namespace package**: it has no
`__init__.py` and no module of its own, and everything in it belongs to
one of three subpackages, which are three published distributions
(ADR 0020). The line between them is *where the code has to run*, not
what it is about:

| Import package | Distribution | What it is | Where it runs |
|---|---|---|---|
| `mcuhome.model` | `mcuhome-model` | the shared vocabulary — device model, registry, the context and manifest formats, the frozen context-ID rule, error types. No build machinery, no third-party dependency | everywhere, including a build server that carries no build logic at all |
| `mcuhome.workbench` | `mcuhome-workbench` | stages 1-3, context creation, the three build methods, the session client, signing | wherever a build is *driven*: the command line, the dashboard, third-party embedders |
| `mcuhome.compiler` | `mcuhome-compiler` | stages 4-5 and the invocation-ABI adapter | inside the build container, out of the mounted SDK |

`mcuhome.workbench.api` is the supported programmatic surface. The
`mcuhome` command itself is a thin shell in its own repository
([mcu-home/cli](https://github.com/mcu-home/cli)) — it parses arguments
and calls in here.

The project files are in [`../packaging/`](../packaging/), not here: the
tree has to sit at the repository root because that root is also the SDK
package a build container mounts and puts on `PYTHONPATH`, so each
distribution reaches up into it rather than holding sources of its own.
All three read one version, from `model/__init__.py`.

## Modules

| Module | Stage | Role |
|---|---|---|
| `workbench/tree.py` | — | config-tree discovery and `<device>` resolution |
| `workbench/loader.py` | 1 | YAML parsing (ruamel, with line/column) and `!secret` |
| `workbench/schema.py` | 2a | typed model of the raw configuration; shape errors |
| `workbench/validate.py` | 2b | cross-references, v0.1 scope gates, Matter conformance |
| `workbench/resolve.py` | 3 | defaults, device-type completion, endpoint numbering, unit conversion |
| `compiler/generate.py` | 4 | the per-device build tree: Matter/channel tables, overlay, Kconfig fragment, CMakeLists, the sysbuild half |
| `compiler/container.py` | 5 | the builder image: which one, which mounts, the `docker run` around the build |
| `compiler/workspace.py` | 5 | west-workspace discovery, prerequisites, the `west build --sysbuild` invocation, per-image artifacts and memory reports |
| `compiler/abi.py` | — | the build container's invocation ABI, and the SDK-side adapter behind it |
| `model/pairing.py` | — | commissioning credentials: SPAKE2+ verifier, QR and manual code, the atomic Kconfig group |
| `workbench/signing.py` | — | the per-user firmware signing key: where it lives, generating one, refusing anything else |
| `model/p256.py` | — | the curve arithmetic `pairing.py` and `signing.py` share, and nothing more |
| `workbench/provision.py` | — | `init-pairing`: draws credentials once and edits them into the device's YAML |
| `model/model.py` | — | the canonical device model and its JSON form |
| `model/registry.py` | — | static tables: clusters, device types, drivers, boards, per-board update scheme and flash layout |
| `model/hashes.py` | — | the one file hash both sides of the build-container contract compute |
| `model/toolchain.py` | — | Zephyr line and blob resolution — the ADR 0013 seam |
| `model/errors.py` | — | the error type and its plain-language rendering |

## The build directory

```
<build dir>/                 default: <tree root>/build/<device>/
├── device-model.json        the canonical model, for inspection
├── app/                     stage 4: a standalone Zephyr application
└── build/                   stage 5: the CMake/ninja tree, one directory per image
    ├── mcuboot/zephyr/      the bootloader
    ├── app/zephyr/          the application, signed and unsigned
    └── merged.hex           both, each at its own offset
```

Everything outside `build/` is meant to be read by a human
(builder-pipeline.md §1.3); everything inside it is machine spoil and can
be deleted at any time. The application is standalone: `west build -b
<board> --sysbuild <build dir>/app` from a west workspace does exactly
what `mcuhome build` does, which is the property that keeps stage 5 thin.
The one argument that cannot travel in the tree is the signing key, which
is a per-user secret and is passed on the command line
(`-DSB_CONFIG_BOOT_SIGNATURE_KEY_FILE=…`, ADR 0015 decision 8).

Stage 5 runs in the **builder image** (ADR 0007,
[../containers/builder/](../containers/builder/README.md)): the host
needs git and docker, the workspace is bind-mounted at its own absolute
path, and the build runs as the calling user. `--method local-dev` compiles in the
west workspace `mcuhome.compiler` is installed in instead — the escape
hatch for MCUHome's own contributors.

The two paths meet at `BuildPlan`: a command plus an environment.
`container.plan_build()` wraps `workspace.west_build_command()` in a
`docker run` and reuses `run_build`, `build_images` and
`parse_image_memory_report` unchanged, so there is one build
orchestration and two ways of reaching a compiler. The container mounts
the signing key as a single read-only file, because `imgtool` runs inside
it and nothing in there should be able to write a key.

## Three rules worth knowing before changing anything here

**Error messages are user interface.** Every rejection says what is
wrong, where (file, line, column, key) and what to do. The tests assert
the text, so changing a message is a deliberate UX change that shows up
in review — not an implementation detail.

**Support is a table row, not code.** New clusters, device types,
peripheral drivers and boards go into `model/registry.py`, with a comment
naming the source of every number. Matter revisions come from CHIP's own
implementation data model, never from the specification scrape shipped
next to it — the sourcing rule is spelled out at the top of that file
and, generated from it, at the top of every emitted table set.

**A board is a table row, not a branch.** ADR 0015 decision 2 puts the
update scheme, the recovery entrance and the whole partition table into
`BoardDef`, and `test_registry.py` reads the source of every other module
of all three packages to prove none of them names a board. The moment one
does, "supporting a new board is a table row plus a bring-up" stops being
true, and nobody finds out until the second board.

**The commissioning identity is one call, never seven lines.**
`model/pairing.py`'s `kconfig_lines()` is the only place in the three
packages that names `CONFIG_CHIP_DEVICE_SPAKE2_*` and friends, and
`test_pairing.py` asserts that no other module does. CHIP takes the passcode and the SPAKE2+
verifier derived from it as unrelated symbols and checks neither against
the other on Zephyr, so a second code path that wrote one of them would
produce firmware that builds, boots, advertises itself and then refuses
every commissioner — with nothing in the log to look at. Adding a symbol
to that identity means adding it to that function.

**The sample is generator output.** `samples/matter-node/src/
mcuhome_config.{c,h}` is what `compiler/generate.py` emits for
`docs/design/examples/00-bmp180-two-endpoints.yaml`, and `pytest`
compares the two byte for byte (ADR 0014). Changing what the generator
emits therefore means regenerating the sample in the same commit — the
recipe is in `../tests_py/README.md`. That coupling is deliberate: it is
what stops the runtime contract, the hardware-verified sample and the
generator from drifting apart.
