# mcuhome-workbench

The Python library behind every MCUHome build: it turns a device's YAML
configuration into signed firmware behind one programmatic interface. It is the
part of the framework the other MCUHome tools embed rather than reimplement.

## What this repository holds

- `mcuhome.workbench.api` — the supported surface: load and validate a device
  configuration, resolve settings, build, and manage a project.
- The configuration pipeline — a device's `main.yaml` parsed, validated and
  resolved into the canonical device model.
- The build context and the two build methods, `local` and `remote`, which drive
  the same build environment from either side.
- The session client that carries a context to a build server and brings the
  unsigned artifacts back.
- Firmware signing — an ECDSA P-256 key drawn per project and MCUboot's
  `imgtool` run over the finished binary.
- The project model — the project marker, its layout version, and the migrations
  that carry a project forward.

## Using it

Depend on `mcuhome-workbench` and import `mcuhome.workbench.api`; every name it
exports is the stable surface, everything else in the package can move without
notice. Resolving a device, loading its model and building it looks like this:

```python
from mcuhome.workbench import api

project, entry = api.find_device("kitchen", env=env, cwd=cwd)
model = api.load_model(entry, project=project)
outcome = await api.run_build(api.BuildRequest(model=model, out_dir=out), method="local")
```

Install the `remote` extra for the build-server client, or `generate` for writing
a device's Zephyr application tree on this machine without building it.

## How it fits into MCUHome

The workbench builds against the firmware SDK in
[mcuhome-sdk](https://github.com/mcu-home/mcuhome-sdk), which also holds the
device model and the code generator this library depends on, and it runs that
build in a build environment on this machine or on a
[mcuhome-buildserver](https://github.com/mcu-home/mcuhome-buildserver) peer. It
is in turn embedded by [mcuhome-cli](https://github.com/mcu-home/mcuhome-cli), a
thin command-line shell over it, and by
[mcuhome-ui](https://github.com/mcu-home/mcuhome-ui), which imports it in-process
so a configuration error arrives in an editor as a marker.

## Working on this repository

Needs Python 3.13, editable installs of `mcuhome-model` and `mcuhome-compiler`
from a checkout of `mcuhome-sdk`, and this package with the `remote` extra — the
session-client tests drive a live `mcuhome-buildserver` peer. The suite lives in
`tests/python/`; run the checks with:

```sh
ruff check . && ruff format --check . && pytest -q
```

CI runs the same two gates on every push and pull request, next to license
(`reuse`), spelling (`codespell`), whitespace and commit-message checks.

## Configuration

Settings are declared once in the `OPTIONS` registry and merged over five layers
by `mcuhome.workbench.api.resolve_settings`: system and user
`configuration.yaml`, the project's `mcuhome.yaml`, `MCUHOME_*` variables, and
the invocation's own arguments. The firmware signing key and a build server's
token live under the project's `secrets/` directory and are referenced from
configuration rather than inlined.

## Security

Firmware is signed on the machine the user controls, never on a build server:
what travels to a builder is the build context and the public half of the key.
The key is drawn per project and kept in the project's `secrets/` directory, or
at a path an embedding application states; MCUboot's published demo key is never
used for a signature. Report a vulnerability as described in the organization's
[security policy](https://github.com/mcu-home/.github/blob/main/SECURITY.md).

## Documentation

- [`docs/adr/`](docs/adr/) — architecture decision records
- [mcuhome-sdk specifications](https://github.com/mcu-home/mcuhome-sdk/tree/main/docs/spec)
  — build environment, context, actions
- [MCUHome on GitHub](https://github.com/mcu-home) — every repository of the
  project

## Contributing and support

Report a bug or propose a change through this repository's
[issue tracker](https://github.com/mcu-home/mcuhome-workbench/issues). The
[contributing rules](https://github.com/mcu-home/.github/blob/main/CONTRIBUTING.md)
apply to every MCUHome repository.

## License

Apache License 2.0 — see [`LICENSE`](LICENSE).
