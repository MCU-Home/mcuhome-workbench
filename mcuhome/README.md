# mcuhome/ — the builder package

The Python side of MCUHome: it reads a YAML device configuration and
produces Zephyr firmware. Design records:
[builder-pipeline.md](../docs/design/builder-pipeline.md),
[yaml-schema.md](../docs/design/yaml-schema.md),
[component-model.md](../docs/design/component-model.md).

```sh
pip install -e '.[dev]'          # from the repository root
mcuhome validate <device>        # stages 1-3, prints the resolved device
mcuhome build <device> --generate-only   # + stage 4, writes the application
mcuhome build <device>           # + stage 5, compiles it into an image
pytest                           # the suite in ../tests_py/
```

## Modules

| Module | Stage | Role |
|---|---|---|
| `cli.py` | — | `argparse` command surface; `validate` and `build` work, `clean` refuses |
| `tree.py` | — | config-tree discovery and `<device>` resolution |
| `loader.py` | 1 | YAML parsing (ruamel, with line/column) and `!secret` |
| `schema.py` | 2a | typed model of the raw configuration; shape errors |
| `validate.py` | 2b | cross-references, v0.1 scope gates, Matter conformance |
| `resolve.py` | 3 | defaults, device-type completion, endpoint numbering, unit conversion |
| `generate.py` | 4 | the per-device build tree: Matter/channel tables, overlay, Kconfig fragment, CMakeLists |
| `workspace.py` | 5 | west-workspace discovery, prerequisites, the `west build` invocation, the memory report |
| `model.py` | — | the canonical device model and its JSON form |
| `registry.py` | — | static tables: clusters, device types, drivers, boards |
| `toolchain.py` | — | Zephyr line and blob resolution — the ADR 0013 seam |
| `errors.py` | — | the error type and its plain-language rendering |

## The build directory

```
<build dir>/                 default: <tree root>/build/<device>/
├── device-model.json        the canonical model, for inspection
├── app/                     stage 4: a standalone Zephyr application
└── build/                   stage 5: the CMake/ninja tree, images under zephyr/
```

Everything outside `build/` is meant to be read by a human
(builder-pipeline.md §1.3); everything inside it is machine spoil and can
be deleted at any time. The application is standalone: `west build -b
<board> -S <snippets> <build dir>/app` from a west workspace does exactly
what `mcuhome build` does, which is the property that keeps stage 5 thin.

Stage 5 runs **natively**, in the west workspace this package is
installed in. ADR 0007's builder container is the intended normal path
and is not implemented yet (phase 2 block D); `--no-native` selects it
and refuses, saying so. The seam is `workspace.plan_build()`: it returns
a command plus an environment, and the container path replaces both and
reuses everything else.

## Three rules worth knowing before changing anything here

**Error messages are user interface.** Every rejection says what is
wrong, where (file, line, column, key) and what to do. The tests assert
the text, so changing a message is a deliberate UX change that shows up
in review — not an implementation detail.

**Support is a table row, not code.** New clusters, device types,
peripheral drivers and boards go into `registry.py`, with a comment
naming the source of every number. Matter revisions come from CHIP's own
implementation data model, never from the specification scrape shipped
next to it — the sourcing rule is spelled out at the top of that file
and, generated from it, at the top of every emitted table set.

**The sample is generator output.** `samples/matter-node/src/
mcuhome_config.{c,h}` is what `generate.py` emits for
`docs/design/examples/00-bmp180-two-endpoints.yaml`, and `pytest`
compares the two byte for byte (ADR 0014). Changing what the generator
emits therefore means regenerating the sample in the same commit — the
recipe is in `../tests_py/README.md`. That coupling is deliberate: it is
what stops the runtime contract, the hardware-verified sample and the
generator from drifting apart.
