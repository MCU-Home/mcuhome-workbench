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
- The package-registry client — mirror discovery, signature verification against
  the project's trust anchor, and the tiered, hash-checked acquisition of the SDK
  and the build-environment packages.
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

## Development — how to work on this repository

This repository has its own virtual environment in `.venv/`; nothing is
installed into the system Python or into another repository's environment.
`bin/` holds the user-facing entry points, `scripts/` the development
tooling: `scripts/test` and `scripts/lint` dispatch the checks — `all` runs
every one, `list` names them, `<name>` runs one — and each check is its own
wrapper in `scripts/test.d/` or `scripts/lint.d/`. The wrappers select
`.venv` themselves (never activate one by hand) and are exactly what CI
runs, one job per check.

Needs Python ≥3.13 and sibling checkouts of `mcuhome-sdk` (`packaging/model`
and `packaging/compiler`), `mcuhome-buildserver` and `mcuhome-packagetool`,
installed editable alongside this package's `remote` extra — the session-client
tests drive a live `mcuhome-buildserver` peer, not a mock.

```sh
python3 -m venv .venv && .venv/bin/pip install \
  -e ../mcuhome-sdk/packaging/model -e ../mcuhome-sdk/packaging/compiler \
  -e ../mcuhome-packagetool -e ../mcuhome-buildserver -e '.[remote]' --group dev
```

[`mcuhome-packagetool`](https://github.com/mcu-home/mcuhome-packagetool) publishes
no distribution, so the version this package is tested and shipped against is
pinned by the commit CI checks out — the `ref:` of its checkout step in
`.github/workflows/ci-test.yml`, and nowhere else. Bumping it means reading that
repository's commits and putting the new SHA there.

```sh
scripts/test all
scripts/lint all
```

The rules that hold across every MCUHome repository — coding standards,
commits, licensing — are in the organization's
[contributing guide](https://github.com/mcu-home/.github/blob/main/CONTRIBUTING.md).

## Configuration

Settings are declared once in the `OPTIONS` registry and merged over five layers
by `mcuhome.workbench.api.resolve_settings`: system and user
`configuration.yaml`, the project's `mcuhome.yaml`, `MCUHOME_*` variables, and
the invocation's own arguments. The firmware signing key and a build server's
token live under the project's `secrets/` directory and are referenced from
configuration rather than inlined.

### Package registries

A registry is a base domain. The workbench asks it where a source is served —
`https://<base-domain>/<source>/mirrors.json` — fetches the signed documents from
a mirror, and verifies them against the project's trust anchor before it reads a
single package name out of them. Two things are configurable per registry, in a
configuration file only:

```yaml
registry:
  packages.mcuhome.org:
    mirrors:
      sdk:
        - /srv/mirrors/sdk
        - https://mirror-2.example.org/sdk/
    untrusted: false
```

`mirrors` **replaces** the served mirror list for the sources it names, and an
entry may be a directory instead of a URL. That is the offline case: an operator
who synchronises a mirror out of band points the workbench at the directory and
builds with no network at all — a local mirror is verified in place, exactly as a
fetched copy is. `untrusted: true` reads a registry without checking anything,
which is only ever right for a registry you run yourself and would rather see
loudly unverified than not at all; every read says so in the build output.

### Trust anchors

An anchor is the root key set a registry's signatures are held against, and it is
configuration, never something downloaded: a client that fetched its anchor would
have reduced every signature below it to "the host said so". One file per base
domain, in `<project>/secrets/trust-anchor/<base-domain>.json`.

For MCUHome's own registry the workbench ships the anchor and writes it there the
first time, so a new project needs no setup. An anchor file that already exists is
never rewritten — if you edited yours, you meant to. For any other registry the
file is yours to create, and a build against a registry without one is refused
with the path to write.

### Which SDK a build uses

A device that names no version in `sources.sdk` is built with the newest patch
release of the SDK minor this workbench was released alongside, and nothing is
written into the device: a device created today is not frozen onto today's
version, and it is not carried forward onto an SDK this workbench has never seen
either. Naming a version — `sources.sdk: sdk/mcuhome-sdk:0.1.9` — pins exactly
that one.

Packages are looked for in the operator's own directories first (`sdk_sources`)
and only then on the registry, and their bytes are checked against the pinned
hash on every path. A machine that already has the package never opens a socket.

## Security

Firmware is signed on the machine the user controls, never on a build server:
what travels to a builder is the build context and the public half of the key.
The key is drawn per project and kept in the project's `secrets/` directory, or
at a path an embedding application states; MCUboot's published demo key is never
used for a signature. Report a vulnerability as described in the organization's
[security policy](https://github.com/mcu-home/.github/blob/main/SECURITY.md).

## Documentation

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
