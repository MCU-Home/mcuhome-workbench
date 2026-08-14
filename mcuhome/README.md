# mcuhome/ — the namespace directory

This directory is a **PEP 420 namespace package**: it has no
`__init__.py` and no module of its own. The `mcuhome.*` namespace spans
three subpackages — three published distributions (ADR 0020) — of which
this repository carries exactly one, `mcuhome.workbench`;
`mcuhome.model` and `mcuhome.compiler` live in
[mcu-home/mcuhome-sdk](https://github.com/mcu-home/mcuhome-sdk) since
the ADR 0024 split. The line between them is *where the code has to
run*, not what it is about:

| Import package | Distribution | What it is | Where it runs |
|---|---|---|---|
| `mcuhome.workbench` | `mcuhome-workbench` | **this repo**: stages 1-3, context creation, the three build methods, the session client, signing | wherever a build is *driven*: the command line, the dashboard, third-party embedders |
| `mcuhome.model` | `mcuhome-model` | the shared vocabulary — device model, registry, the context and manifest formats, the frozen context-ID rule, error types | everywhere, including a build server that carries no build logic at all |
| `mcuhome.compiler` | `mcuhome-compiler` | stages 4-5 and the invocation-ABI adapter | inside the build container, out of the mounted SDK |

`mcuhome.workbench.api` is the supported programmatic surface; every
other module here is an implementation detail. The `mcuhome` command
itself is a thin shell in its own repository
([mcu-home/cli](https://github.com/mcu-home/cli)) — it parses arguments
and calls in here.

The project file is the repository root's `pyproject.toml` (ADR 0024):
this distribution builds from the root, and the workbench versions
independently of the SDK repository
(`mcuhome/workbench/__init__.py::__version__`).

```sh
# from the repository root, with mcuhome-sdk cloned next to this repo:
pip install -e ../mcuhome-sdk/packaging/model \
            -e ../mcuhome-sdk/packaging/compiler \
            -e '.[remote]'
pytest                           # the suite in ../tests_py/
```

## Modules

| Module | Stage | Role |
|---|---|---|
| `workbench/api.py` | — | the supported programmatic surface over everything below |
| `workbench/project.py` | — | the project directory (ADR 0022): marker, layout, `init`, secrets hygiene, `<device>` resolution |
| `workbench/configuration.py` | — | the five-layer option model (ADR 0022): registry, precedence, origins |
| `workbench/loader.py` | 1 | YAML parsing (ruamel, with line/column) and `!secret` |
| `workbench/schema.py` | 2a | typed model of the raw configuration; shape errors |
| `workbench/validate.py` | 2b | cross-references, v0.1 scope gates, Matter conformance |
| `workbench/resolve.py` | 3 | defaults, device-type completion, endpoint numbering, unit conversion |
| `workbench/configschema.py` | — | the `main.yaml` schema as data (JSON Schema) |
| `workbench/scaffold.py` | — | `mcuhome new`: a starter device configuration |
| `workbench/provision.py` | — | `init-pairing`: drawing a device's commissioning credentials |
| `workbench/contextdir.py` | — | build-context creation and locking (ADR 0018) |
| `workbench/resolve_pins.py` | — | SDK pin resolution against an index or directory |
| `workbench/buildmethods.py` | — | the three build methods behind `run_build` (E53/E64) |
| `workbench/sessionclient.py` | — | the remote method's session-protocol client (ADR 0019) |
| `workbench/imgtool.py` | — | detached signing over the §7.2.1 build report |
| `workbench/signing.py` | — | the per-project signing key and its refusals (ADR 0015 §8) |
| `workbench/userpaths.py` | — | the process-boundary seam for `$HOME`-shaped lookups |

## Two rules worth knowing before changing anything here

**Error messages are user interface.** Every rejection says what is
wrong, where (file, line, column, key) and what to do. The tests assert
the text, so changing a message is a deliberate UX change that shows up
in review — not an implementation detail.

**The compiler is resolved at call time, never imported.** A dashboard
install must not carry a toolchain (ADR 0017 §2, ADR 0020 decision 3):
the edge to `mcuhome.compiler` goes through
`importlib.import_module` and refuses in words when the distribution is
absent. `tests_py/test_packaging_workbench.py` reads the dependency
arrows out of the syntax tree, so a plain `import` is a test failure.
