# tests_py/

Python tests for the builder package (`mcuhome/`), run with pytest:

```sh
pip install -e '.[dev]'
pytest
```

**Why not `tests/`?** That directory holds Zephyr twister suites
(`testcase.yaml` per subdirectory) and twister recurses into whatever it
is pointed at, so a Python test tree living there would be walked by
twister and a C suite would be collected by pytest. Two runners, two
roots, no overlap. `pyproject.toml` pins pytest to this directory
(`testpaths`), so a bare `pytest` from the repo root does the right
thing.

These tests need neither Zephyr nor a west workspace and run in about a
second — they are the fast half of the strategy in
[`docs/design/builder-pipeline.md`](../docs/design/builder-pipeline.md)
§9.

| File | Covers |
|---|---|
| `test_tree.py` | config-root discovery, device resolution (name, folder, bare file) |
| `test_loader.py` | YAML parsing and `!secret` resolution, including their error messages |
| `test_schema.py` | shape errors: unknown keys, wrong types, malformed durations |
| `test_validate.py` | every v0.1 scope gate and cross-reference check, message **and** location |
| `test_pairing.py` | commissioning: the CHIP vectors, the atomic Kconfig group, `init-pairing` |
| `test_registry.py` | the per-board update scheme and flash layout, and that no module branches on a board name (ADR 0015) |
| `test_signing.py` | the per-user signing key: where it is, what it is, and how the refusals read (ADR 0015 §8) |
| `test_examples.py` | the design examples in `docs/design/examples/` |
| `test_model_golden.py` | the canonical model of `00-bmp180-two-endpoints.yaml`, byte-exact |
| `test_generate.py` | stage 4: every generated artifact, byte-exact, plus its error paths |
| `test_workspace.py` | stage 5 on the host: workspace discovery, prerequisites, the sysbuild command, per-image artifacts and memory reports |
| `test_container.py` | stage 5 in the image: image tag, mounts, environment, the three refusals |
| `test_api.py` | the supported programmatic surface (`mcuhome.api`) and the serialized shape of an error |
| `test_manifest.py` | `build-manifest.json`: its fields, its determinism, and the signing parameters it states |
| `test_imgtool.py` | detached signing: the command is Zephyr's own, and two signings of one image differ only in the signature |
| `test_export.py` | the registry and the `main.yaml` JSON Schema, golden and against the parser |
| `test_scaffold.py` | `mcuhome new`: what it writes, what it refuses, and that init-pairing then validate works on it |

The command-surface tests (exit codes, summary output, `--json`
documents) live with the command: `tests/test_cli.py` in the
[mcu-home/cli](https://github.com/mcu-home/cli) repository, which is
where the `mcuhome` command itself moved.

The one test that runs an external program is the detached-signing
equivalence proof in `test_imgtool.py`, which invokes `imgtool` over a
few kilobytes of synthetic image and skips itself where imgtool is not
installed. That is signing, not building: it takes milliseconds and needs
no toolchain.

**No build ever runs here, docker never runs, and no test touches the
developer's own signing key.** An autouse fixture in `conftest.py` points
`XDG_CONFIG_HOME` at `tmp_path`, because `mcuhome build` generates a real
private key on first need and a suite that reached the real one would
either read a secret or create one outside a temporary directory.

**No build ever runs here, and neither does docker.** `test_workspace.py`
and `test_container.py` (plus the `build` tests in the cli repository)
cover everything stage 5 decides *before* the compiler starts and mock
the subprocess itself — `container.plan_build()` takes its process
runner as an argument for exactly that reason, and an autouse fixture in
`conftest.py` makes the real one raise, so a test that forgets to stub
stage 5 fails instead of starting a Matter build. Compiling a Matter node takes
minutes, a toolchain and a few gigabytes of image; that belongs to
twister and to hardware verification, not to a suite whose whole value is
running in a second.

## Golden files

`data/golden/` holds the byte-exact expected device model, devicetree
overlay, Kconfig fragment, application `CMakeLists.txt`,
`CHIPProjectConfig.h` wrapper and the three sysbuild artifacts
(`sysbuild.conf` plus the bootloader image's `.conf` and `.overlay`),
plus the two documents the builder exports as its contract with the
dashboard — `registry.json` and `main.schema.json`. The two generated C files are not
duplicated here: **the committed sample is the golden file** for those
(ADR 0014), so `test_generate.py` compares fresh generator output against
`samples/matter-node/src/mcuhome_config.{c,h}` directly.

Regenerate deliberately, never automatically — from the repository root:

```sh
# the device model
python -c "from pathlib import Path; from mcuhome.api import ConfigTree, load_model; \
p = Path('docs/design/examples/00-bmp180-two-endpoints.yaml').resolve(); \
print(load_model(p, tree=ConfigTree(root=p.parent, discovered=False)).to_json(), end='')" \
  > tests_py/data/golden/00-bmp180-two-endpoints.device-model.json

# the stage-4 artifacts, including the sample's C files
mcuhome build docs/design/examples/00-bmp180-two-endpoints.yaml \
  --build-dir /tmp/bmp180-node --generate-only
cp /tmp/bmp180-node/app/src/mcuhome_config.[ch] samples/matter-node/src/
cp /tmp/bmp180-node/app/prj.conf \
   tests_py/data/golden/00-bmp180-two-endpoints.prj.conf
cp /tmp/bmp180-node/app/CMakeLists.txt \
   tests_py/data/golden/00-bmp180-two-endpoints.CMakeLists.txt
cp /tmp/bmp180-node/app/boards/nrf7002dk_nrf5340_cpuapp.overlay \
   tests_py/data/golden/00-bmp180-two-endpoints.overlay
cp /tmp/bmp180-node/app/include/CHIPProjectConfig.h \
   tests_py/data/golden/00-bmp180-two-endpoints.CHIPProjectConfig.h
cp /tmp/bmp180-node/app/sysbuild.conf \
   tests_py/data/golden/00-bmp180-two-endpoints.sysbuild.conf
cp /tmp/bmp180-node/app/sysbuild/mcuboot.conf \
   tests_py/data/golden/00-bmp180-two-endpoints.mcuboot.conf
cp /tmp/bmp180-node/app/sysbuild/mcuboot.overlay \
   tests_py/data/golden/00-bmp180-two-endpoints.mcuboot.overlay

# the two exported contract documents
mcuhome schema config   -o tests_py/data/golden/main.schema.json
mcuhome schema registry -o tests_py/data/golden/registry.json
```

Regenerating the sample's C files is not optional bookkeeping: it is how
the runtime contract, the hardware-verified sample and the generator stay
in lockstep. Build the sample afterwards — the generated tables reference
the sensor's devicetree node, so a renamed peripheral breaks the board
overlay next to them.

Error **text** is asserted on purpose: validation messages are user
interface (builder-pipeline.md §9). If a message changes, that is a
deliberate UX change and the test is where it gets reviewed.

## Foreign vectors

`test_pairing.py` is the one suite whose expected values are not
MCUHome's own output: every SPAKE2+ verifier, QR payload and manual code
in it comes from the pinned connectedhomeip checkout, with the file it
was taken from named in a comment. Reproducing somebody else's numbers is
the only way to know that a code this builder prints is a code a real
controller accepts — a golden file of our own output would only prove we
are consistent. The last vector is stronger still: the two codes of the
nRF7002-DK that was commissioned into a production Home Assistant.
