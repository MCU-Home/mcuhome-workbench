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
pytest                           # the suite in ../tests_py/
```

## Modules

| Module | Stage | Role |
|---|---|---|
| `cli.py` | — | `argparse` command surface; `validate` and generating `build` work, `clean` refuses |
| `tree.py` | — | config-tree discovery and `<device>` resolution |
| `loader.py` | 1 | YAML parsing (ruamel, with line/column) and `!secret` |
| `schema.py` | 2a | typed model of the raw configuration; shape errors |
| `validate.py` | 2b | cross-references, v0.1 scope gates, Matter conformance |
| `resolve.py` | 3 | defaults, device-type completion, endpoint numbering, unit conversion |
| `generate.py` | 4 | the per-device build tree: Matter/channel tables, overlay, Kconfig fragment, CMakeLists |
| `model.py` | — | the canonical device model and its JSON form |
| `registry.py` | — | static tables: clusters, device types, drivers, boards |
| `toolchain.py` | — | Zephyr line and blob resolution — the ADR 0013 seam |
| `errors.py` | — | the error type and its plain-language rendering |

Stage 5 (compiling the generated application, in the builder container)
is not implemented yet: `mcuhome build` writes the application and then
says so.

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
