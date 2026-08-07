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
| `test_examples.py` | the design examples in `docs/design/examples/` |
| `test_model_golden.py` | the canonical model of `00-bmp180-two-endpoints.yaml`, byte-exact |
| `test_generate.py` | stage 4: every generated artifact, byte-exact, plus its error paths |
| `test_cli.py` | command surface, exit codes, summary output |

## Golden files

`data/golden/` holds the byte-exact expected device model, devicetree
overlay, Kconfig fragment and application `CMakeLists.txt`. The two
generated C files are not duplicated here: **the committed sample is the
golden file** for those (ADR 0014), so `test_generate.py` compares fresh
generator output against `samples/matter-node/src/mcuhome_config.{c,h}`
directly.

Regenerate deliberately, never automatically — from the repository root:

```sh
# the device model
python -c "from pathlib import Path; from mcuhome.cli import load_device_model; \
from mcuhome.tree import ConfigTree; \
p = Path('docs/design/examples/00-bmp180-two-endpoints.yaml').resolve(); \
print(load_device_model(p, tree=ConfigTree(root=p.parent, discovered=False)).to_json(), end='')" \
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
```

Regenerating the sample's C files is not optional bookkeeping: it is how
the runtime contract, the hardware-verified sample and the generator stay
in lockstep. Build the sample afterwards — the generated tables reference
the sensor's devicetree node, so a renamed peripheral breaks the board
overlay next to them.

Error **text** is asserted on purpose: validation messages are user
interface (builder-pipeline.md §9). If a message changes, that is a
deliberate UX change and the test is where it gets reviewed.
