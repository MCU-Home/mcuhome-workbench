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
fetched copy is.

`untrusted: true` reads a registry without checking anything: no signature, no
freshness, no key set. It is only ever right for a registry you run yourself and
would rather see loudly unverified than not at all, and it says so in the build
log at every read — once per index and once per package. A trust anchor sitting
next to `untrusted: true` is **ignored**, and the warning says that too: the
setting is the project's statement, and a file does not overrule it.

Registries are only read when they are needed. A build whose packages are already
in the operator's own directories asks nothing, needs no anchor, and cannot be
stopped by a missing one.

### Trust anchors

An anchor is the root key set a registry's signatures are held against, and it is
configuration, never something downloaded: a client that fetched its anchor would
have reduced every signature below it to "the host said so". One file per base
domain, in `<project>/secrets/trust-anchor/<base-domain>.json`.

The anchors are written when a project is created — `mcuhome project init` puts
the one this workbench ships for MCUHome's own registry into place — and at no
other moment. A build that finds the file missing refuses and says which file to
create; it does **not** install one, for MCUHome's registry as little as for
anybody else's, because a tool that acquired its own trust root while about to
download something has verified nothing. If yours went missing, run
`mcuhome project init . --force` over the project again. An anchor file that
already exists is never rewritten — if you edited yours, you meant to. For any
other registry the file is yours to create.

### Which SDK a build uses

A device that names no version in `sources.sdk` is built with the newest release
of the SDK minor this workbench was released alongside, and nothing is written
into the device: a device created today is not frozen onto today's version, and
it is not carried forward onto an SDK this workbench has never seen either.

That default takes **pre-releases** — every MCUHome package in the 0.1 line is a
`.devN` release, so a default that skipped them would resolve to nothing at all.
It is the one place that does: a version you state yourself follows the ordinary
rule, where a dev release satisfies a constraint only if the constraint names one.
So `sources.sdk: sdk/mcuhome-sdk:0.1.9` pins exactly `0.1.9` and will not quietly
become `0.1.9.dev1`; write `:0.1.9.dev1` if that is what you want. Either way the
minor bound holds — the default never crosses into 0.2.

Packages are looked for in the operator's own directories first (`sdk_sources`)
and only then on the registry, and their bytes are checked against the pinned
hash on every path. A machine that already has the package never opens a socket.
A package published per architecture is named once — the index says which
concrete package that name stands for on this host, and the mapping is checked
against the members it points at before anything is fetched.

### Which build environment a build uses

The same rule, one step further along: a device that names no version in
`sources.build_workspace` or `sources.build_tools` is built with the environment
the **resolved SDK release** was built and tested with. Every SDK release carries
a `build-environment.lock.json` stating those two versions, and their hashes come
from the same package index the SDK came from — so a device without any
`sources.*` entry gets an SDK and an environment that were released together, and
neither is written into the device.

Each entry overrides its own package and nothing else:

```yaml
sources:
  build_tools: build-tools/mcuhome-build-tools:0.1.10.dev1
```

The tools package is published per architecture and the bare family name is the
normal pin: it resolves to this host's package, and pinning the family still
pins every platform's bytes, which is what lets one build context produce the
same firmware on an amd64 host and on an arm64 one. Naming one platform's
package outright (`mcuhome-build-tools_linux-amd64`) is allowed and means exactly
that — a host of another architecture refuses rather than substituting something.

A reference may state a hash as well as a version:

```yaml
sources:
  build_workspace: build-workspace/mcuhome-build-workspace:0.1.10.dev1@sha256:7c31…
```

That decides the whole pin, and nothing is looked up at all — no release lock, no
index. It is what an air-gapped machine states when it has the archives but no
package index for them.

Everything else needs an index that lists the two packages, because a hash can
come from nowhere else: put them in one of the operator's own package
directories, or configure the registry.

### The build environment store

A build that does not run in a container needs its build environment as files on
this machine. The workbench unpacks the environment's packages into a store under
the user's own cache home:

```
${XDG_CACHE_HOME:-~/.cache}/mcuhome/build-environments/<package name>-<version>/
```

One directory per package and version, so two projects on different versions do
not disturb each other and neither is ever unpacked twice. Unpacking happens
beside the entry and the finished tree is moved into place in one step, an entry
is finalized once — the build's Python environment created from the wheels the
package carries, offline; west's configuration checked; git told that the
workspace's repositories are not foreign — and is then **frozen read-only**,
because every build using that version shares it, including builds running at
the same time. A build that needs to change a tree works on a copy.

**Clearing it.** The store is a cache: deleting it costs the next build the
unpacking time and nothing else. Since the entries are read-only, deletion takes
two commands:

```console
$ store="${XDG_CACHE_HOME:-$HOME/.cache}/mcuhome/build-environments"
$ chmod -R u+w "$store"
$ rm -rf "$store"
```

Single entries go the same way — `chmod -R u+w`, then `rm -rf`, on the one
directory.

**The host's Python.** The environment's tools package carries the Python
packages a build needs as wheels, and some of them are compiled against one
version of Python — the one current Debian stable ships, which is what the
package is built with. MCUHome checks that before it creates anything and refuses
with the version it needs; nothing is downloaded or compiled to paper over the
difference. Run MCUHome on that version, or build in a container, where the
question does not arise.

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
