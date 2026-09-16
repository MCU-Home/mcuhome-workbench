# mcuhome/ — the namespace directory

This directory is a **PEP 420 namespace package**: it has no
`__init__.py` and no module of its own. The `mcuhome.*` namespace spans
three subpackages — three published distributions — of which
this repository carries exactly one, `mcuhome.workbench`;
`mcuhome.model` and `mcuhome.compiler` live in
[mcu-home/mcuhome-sdk](https://github.com/mcu-home/mcuhome-sdk) since
the repository split. The line between them is *where the code has to
run*, not what it is about:

| Import package | Distribution | What it is | Where it runs |
|---|---|---|---|
| `mcuhome.workbench` | `mcuhome-workbench` | **this repo**: stages 1-3, context creation, the two build targets, the session client, signing | wherever a build is *driven*: the command line, the dashboard, third-party embedders |
| `mcuhome.model` | `mcuhome-model` | the shared vocabulary — device model, registry, the context and manifest formats, the frozen context-ID rule, error types | everywhere, including a build server that carries no build logic at all |
| `mcuhome.compiler` | `mcuhome-compiler` | stages 4-5, and the builder a build environment runs | inside the build environment, out of the mounted SDK |

`mcuhome.workbench.api` is the supported programmatic surface; every
other module here is an implementation detail. The `mcuhome` command
itself is a thin shell in its own repository
([mcu-home/mcuhome-cli](https://github.com/mcu-home/mcuhome-cli)) — it parses arguments
and calls in here.

The project file is the repository root's `pyproject.toml`:
this distribution builds from the root, and the workbench versions
independently of the SDK repository
(`mcuhome/workbench/__init__.py::__version__`).

```sh
# from the repository root, with mcuhome-sdk cloned next to this repo:
pip install -e ../mcuhome-sdk/packaging/model \
            -e ../mcuhome-sdk/packaging/compiler \
            -e '.[remote]'
pytest                           # the suite in ../tests/python/
```

## Modules

| Module | Stage | Role |
|---|---|---|
| `workbench/api.py` | — | the supported programmatic surface over everything below |
| `workbench/project.py` | — | the project directory: marker, layout, `init`, secrets hygiene, `<device>` resolution |
| `workbench/configuration.py` | — | the five-layer option model: registry, precedence, origins, builder selection |
| `workbench/builders.py` | — | named builders: vocabulary, merge-by-name, selection |
| `workbench/loader.py` | 1 | YAML parsing (ruamel, with line/column), `!secret` and `!file` |
| `workbench/schema.py` | 2a | typed model of the raw configuration; shape errors |
| `workbench/validate.py` | 2b | cross-references, v0.1 scope gates, Matter conformance |
| `workbench/resolve.py` | 3 | defaults, device-type completion, endpoint numbering, unit conversion |
| `workbench/configschema.py` | — | the `main.yaml` schema as data (JSON Schema) |
| `workbench/scaffold.py` | — | `mcuhome device new`: a starter device configuration |
| `workbench/provision.py` | — | `create-matter-pairing`: drawing a device's commissioning credentials |
| `workbench/contextdir.py` | — | build-context creation and locking |
| `workbench/resolve_pins.py` | — | the pin chain — SDK, then the build workspace and build tools its meta files require — resolved against an index or a directory |
| `workbench/build.py` | — | the two build targets behind `build_firmware` |
| `workbench/containerbuild.py` | — | the `container` mode: one fresh container per step, in the image the context's packages select |
| `workbench/resolve_image.py` | — | which container image delivers a package set: the repository list, the four pin forms, the label check |
| `workbench/subprocessbuild.py` | — | the `subprocess` mode: the unpacked environment run as a child process |
| `workbench/devworkspace.py` | — | `build.dev_workspace`: a west workspace of your own as the environment |
| `workbench/buildenvstore.py` | — | the store the subprocess mode unpacks, finalizes and freezes environments in |
| `workbench/sessionclient.py` | — | the ``remote`` target's session-protocol client |
| `workbench/imgtool.py` | — | detached signing over the build report, whose shape the build actions document defines |
| `workbench/signing.py` | — | the per-project signing key and its refusals |
| `workbench/userpaths.py` | — | the process-boundary seam for `$HOME`-shaped lookups |

## Two rules worth knowing before changing anything here

**Error messages are user interface.** Every rejection says what is
wrong, where (file, line, column, key) and what to do. The tests assert
the text, so changing a message is a deliberate UX change that shows up
in review — not an implementation detail.

**The compiler is resolved at call time, never imported.** A dashboard
install must not carry a toolchain:
the edge to `mcuhome.compiler` goes through
`importlib.import_module` and refuses in words when the distribution is
absent. `tests/python/test_packaging_workbench.py` reads the dependency
arrows out of the syntax tree, so a plain `import` is a test failure.
