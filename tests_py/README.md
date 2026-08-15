# tests_py/

Python tests for `mcuhome.workbench` — the one package this repository
publishes (ADR 0024) — run with pytest:

```sh
# from the repository root, with the SDK siblings cloned next to it:
pip install -e ../mcuhome-sdk/packaging/model \
            -e ../mcuhome-sdk/packaging/compiler \
            -e '.[remote]'
pytest
```

The workbench install is **editable** and the suite depends on it: the
whole-package invariants read the source of every module of the
package, and `conftest.package_modules()` refuses to run against
modules that are not the ones in this checkout. `mcuhome-model` and
`mcuhome-compiler` are ordinary installed dependencies here — their own
suite lives in
[mcu-home/mcuhome-sdk](https://github.com/mcu-home/mcuhome-sdk)'s
`tests_py/`, and files that were split along that boundary carry a
`_workbench` suffix on this side.

`test_sessionclient.py` drives the remote method's client against the
**real** `mcuhome-build-server` over a real socket rather than a mock,
so it needs that peer installed too (`pip install -e ../build-server`);
without it the file skips itself with a reason naming what is missing.

These tests need neither Zephyr nor a west workspace and run in a few
seconds. `pyproject.toml` pins pytest to this directory (`testpaths`),
so a bare `pytest` from the repo root does the right thing.

| File | Covers |
|---|---|
| `test_project.py` | the project marker and its bootstrap ladder, layout, `mcuhome init`, secrets hygiene, device resolution (name, folder, bare file) |
| `test_configuration.py` | the five-layer option model: precedence, origins, per-option channels, `config print` data |
| `test_builders.py` | named builders (ADR 0023): parsing, merge-by-name, selection, credentials |
| `test_loader.py` | YAML parsing, `!secret` and `!file` resolution, including their error messages |
| `test_schema.py` | shape errors: unknown keys, wrong types, malformed durations |
| `test_validate.py` | every v0.1 scope gate and cross-reference check, message **and** location |
| `test_examples.py` | the example configurations in `data/examples/` (copies of the SDK's design examples) |
| `test_model_golden.py` | the cross-repo contract: stages 1-3 still produce the golden `device-model.json` the SDK's suite consumes |
| `test_scaffold.py` | `mcuhome new`: the starter configuration and its refusals |
| `test_api.py` | the supported surface: exports, `validate_device`, `error_dicts`, the version |
| `test_buildmethods.py` | the three build methods behind `run_build`; the call-time compiler edge |
| `test_localbuild.py` | the `local` method's composition (`compose_local_build`) against a scripted container backend |
| `test_resolve_pins.py` | SDK pin resolution: index, directory, refusals |
| `test_sessionclient.py` | the session protocol end to end against a live build server |
| `test_signing.py` | the per-project signing key: where it is, what it is, how the refusals read (ADR 0015 §8) |
| `test_imgtool.py` | detached signing over the §7.2.1 build report |
| `test_context_workbench.py` | context creation/locking, the frozen context-ID rule (ADR 0018 §6) |
| `test_export_workbench.py` | registry data and the `main.yaml` JSON schema as data |
| `test_ota_workbench.py` | the Matter OTA image wrap around a freshly signed image |
| `test_pairing_workbench.py` | `matter-pairing` plus the identity-symbol invariant (no workbench module spells `CONFIG_CHIP_DEVICE_SPAKE2_*`) |
| `test_userpaths_workbench.py` | the process-boundary invariant: no module reads `$HOME`/`os.environ` outside the seam |
| `test_packaging_workbench.py` | the root `pyproject.toml`: name, version source, dependency arrows, extras |
