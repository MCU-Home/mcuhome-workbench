# mcuhome-workbench

The Python library behind every MCUHome build: it turns a device's YAML
configuration into signed firmware behind one programmatic interface. It is the
part of the framework the other MCUHome tools embed rather than reimplement.

## What this repository holds

- `mcuhome.workbench.api` — the supported surface: load and validate a device
  configuration, resolve settings, build, and manage a project.
- The configuration pipeline — a device's `main.yaml` parsed, validated and
  resolved into the canonical device model.
- The build context and the two axes a build is placed on: the target
  (`local`, `remote`) and, for a local build, the mode (`container`,
  `subprocess`) — the same build environment driven from either side.
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
notice. [`docs/api.md`](docs/api.md) is the reference for it — every exported
name with its signature and what it raises, the options, the environment
variables, the files and the documents — and this section is the narrative
introduction to the same thing. Resolving a device, loading its model and
building it looks like this:

```python
from mcuhome.workbench import api

project, entry = api.resolve_device("kitchen", env=env, cwd=cwd)
model = api.load_model(entry, project=project)
request = api.BuildRequest(model=model, out_dir=out)
result = await api.build_firmware(request, target="local")
```

A client that comes back to a build directory later — a second process, a
dashboard that restarted, a run tomorrow — reads what is in it instead of
building again to find out. Every build leaves a record behind, whatever its
verdict:

```python
record = api.read_build(out)  # None where no build was ever made
if record and not record.busy:  # somebody else may be working in there
    print(record.device, record.artifacts, record.signed)
    api.clean_build(out, device=record.device)
```

`read_build` verifies nothing and re-computes no hash — it states what the
build declared — and `clean_build` removes what a build wrote and leaves
everything else, holding the directory while it does. Before a build starts,
`api.build_steps(target=…, options=…)` says which steps it will report through
`on_step`, so a client can lay its progress out in advance;
`api.read_pairing(entry, project=project)` shows a device's commissioning codes
without drawing new ones.

A device is its folder, and everything MCUHome keeps for it is keyed on that
name, so renaming or deleting one is a call rather than a `mv`:

```python
api.rename_device("kitchen", project=project, to="hallway")
api.delete_device("attic", project=project, keep_secrets=True)
```

`rename_device` moves the device folder with its patches, writes the new name
into the file — a device whose file disagrees with its folder is refused when
it is loaded — moves the device's own secrets, and removes the build
directory, because build output names the device inside its own report.
`delete_device` removes the device, its build output and its secrets, and
`keep_secrets` holds on to commissioning credentials that cannot be drawn
again. Both hold the device's build directory while they work, so a build in
flight refuses them instead of losing its output, and both answer a result
naming the device, the two names or whether the secrets were kept, and every
path they changed.

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
It is a library and has no command of its own — `mcuhome-cli` is the command
line over it — so `scripts/` holds the development tooling and nothing
else: `scripts/test` and `scripts/lint` dispatch the checks — `all` runs
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

Settings are declared once in the `OPTIONS` registry and merged over the layers
by `mcuhome.workbench.api.resolve_settings`: a program's own defaults, system and
user `configuration.yaml`, the project's `mcuhome.yaml`, `MCUHOME_*` variables,
and the invocation's own arguments. The firmware signing key and a build
server's token live under the project's `secrets/` directory and are referenced
from configuration rather than inlined.

**Every option states the area it belongs to**, and the dot in its name is a
real level in every spelling of it: a section in a file, an underscore in the
variable, a dash in the flag. The areas are `project` (where the work lives),
`signing` (the firmware key and the program that uses it), `build` (how this
machine builds), `builder` (the named builders a build may run at), and
`registry` (package registries). Everything that describes **how this machine
builds** lives under `build`:

```yaml
build:
  target: local             # MCUHOME_BUILD_TARGET — local | remote
  mode: subprocess          # MCUHOME_BUILD_MODE — container | subprocess
  env_store: /srv/mcuhome/build-environments
```

The two are two axes and therefore two keys: `build.target` says **where** a
build runs — on this machine or on a build server — and `build.mode` says how
**this** machine executes a local build. A client does not get to tell a build
server whether to start a container, so one word could never have answered
both.

One declaration produces every spelling: `build.mode` is `MCUHOME_BUILD_MODE`
in the environment and `--build-mode` on a command line, and because an area is
always one word the flag splits back into its key without a table. `mcuhome
config print` shows every option with the layer it came from, and `mcuhome
config set build.mode subprocess --scope user` writes the section for you.

Named builders are a map under `builder`, keyed by the builder's name, and
`build.builder` names the one a plain build uses:

```yaml
builder:
  attic:
    target: remote          # local | remote
    server: 10.0.0.5:8291   # the token lives in secrets/builder/attic.yaml
build:
  builder: attic
```

One entry of such a map is written key by key, so a builder and a registry need
no hand-edited YAML either: `mcuhome config set builder.attic.target remote`,
`mcuhome config set registry.packages.mcuhome.org.mirrors.sdk /srv/mirror`, and
`mcuhome config unset` takes the entry with its last key and the map with its
last entry.

A key that used to be an option is **refused where it is written**, naming the
option it is today: `ccache_dir` is `build.cache_root`, `default_builder` is
`build.builder`, `builders` is the map `builder` (with `type:` as `target:` and
`image:` as `container_image:`), `signing_key` is `signing.key` and `project_dir`
is `project.dir`. A configuration file carries no version, so nothing can
rewrite it for you — the refusal states the line to write instead. A retired
`MCUHOME_*` variable only warns, so a stale export cannot block the command that
would fix it.

### Package registries

A registry is a base domain. The workbench asks it where a source is served —
`https://<base-domain>/<source>/mirrors.json` — fetches the signed documents from
a mirror, and verifies them against the project's trust anchor before it reads a
single package name out of them. What a registry is configured with — its
mirrors, whether it is checked at all, and which anchor it is checked against —
lives in a configuration file only:

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

An anchor that is deployed with the machine rather than kept in a project is
named in the registry's own entry:

```yaml
registry:
  packages.example.org:
    anchor: /etc/mcuhome/anchors/packages.example.org.json
```

It replaces the project's file for that one registry — it is not a fallback for
a missing one, and a path that names nothing is refused rather than ignored,
because a build must not end up checking a registry against something other
than what you chose. It is stated per registry on purpose: one setting that
moved every anchor at once would move MCUHome's own along with your private
one.

### Which packages a device is built from

`sources:` is a top-level block of the **device** file, beside `device:` and
`node:`, and it names the packages the build fetches:

```yaml
device:
  name: bench-node
  board: nrf7002dk/nrf5340/cpuapp

sources:
  sdk: sdk/mcuhome-sdk:0.1.9
  build_workspace: build-workspace/mcuhome-build-workspace:0.1.0
  build_tools: build-tools/mcuhome-build-tools
```

The block is optional, every entry in it is optional, and each entry overrides
its own package and nothing else. **Nothing writes this block for you**: a
device created by `mcuhome device new` carries no `sources:` at all, and a build
never adds one. Write an entry when you want a device pinned; leave it out — the
normal case — and the version is resolved at build time, as the next two
sections describe.

The form of a reference is
`[<registry-host>/][<source>/]<package>[:<constraint>][@sha256:<hash>]`, the
spelling you already know from docker, with a **version constraint** where
docker has a tag:

```yaml
sources:
  sdk: sdk/mcuhome-sdk:>=0.1.9,<0.2                      # a range
  build_workspace: mcuhome-build-workspace:~=0.1.0       # patch releases of 0.1
  build_tools: "build-tools/mcuhome-build-tools:0.1.4"   # that version, exactly
```

The registry host and the source shelf default to the ones the package is
normally served from, so a package name alone resolves.
Everything but the package name is optional, and so is the package name: a value
that starts with the separator says the rest about the package the entry is
already about — `":~=0.1.0"` narrows the range, `"@sha256:<hash>"` selects one
archive outright — exactly as `sources.container_image` takes a bare `:tag`. A
bare version (`0.1.4`) is the exact pin it looks like; anything else is a
[PEP 440](https://peps.python.org/pep-0440/) specifier (`~=0.1.0`, `>=0.1,<0.2`,
`==0.1.*`). A registry in front of the reference points that one package at
another host, which is then read with that host's own trust anchor and mirrors
(`registry.<base-domain>.*` in `mcuhome.yaml`). Quote any value that starts with
a colon or contains one, as YAML asks.

A device can also carry **patches** for the trees it is built from. They live in
the device's own folder, `devices/<device>/patches/<layer>/NNNN-description.patch`:
the layer names the tree the patch applies to (`zephyr`, `sdk`, `chip`, whatever
the build environment knows), and the number is the order they are applied in
inside that layer. Every
build of that device carries them, and there is no flag to switch it on — the
folder is the statement. Patches travel in the build context beside the device
model and are hashed into its id like every other file in it, so a build with a
patch is a different build from the same device without one; an empty folder, or
none at all, changes nothing. Two things are refused rather than guessed: a file
that is not inside a layer folder, because nothing could say which tree it
belongs to, and a patched device built against a west workspace you maintain
yourself (`build.dev_workspace`), because those trees are yours and MCUHome does
not patch them. The folder is the device's name: a device file that calls itself
something else is refused when it is loaded, because everything MCUHome writes
for a device is keyed on that one name — `mcuhome device new` writes both the
same, and a file you point at from outside a project is free to call itself
anything.

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

Packages are looked for in the operator's own directories first — one key per
package kind (`build.sdk_sources`, `build.workspace_sources`,
`build.tools_sources`), and no kind under another's — and only then on the
registry, and their bytes are checked
against the pinned hash on every path. A machine that already has the package
never opens a socket. A package published per architecture is named once — the
index says which concrete package that name stands for on this host, and the
mapping is checked against the members it points at before anything is fetched.

### Which build environment a build uses

The same rule, one step further along, and it is a **chain**. The SDK, the build
workspace and the build tools are released on lines of their own, and each one
states a *range* of the next rather than a version: the resolved SDK release's
`meta.json` names which build workspaces it was built and tested with, the
workspace package that resolves to names which build tools it needs, and the
tools end the chain. Each range resolves to the newest published version inside
it, which is then pinned exactly — name, version and hash — into the build
context. So a workspace release that fixes something reaches an existing device
without the SDK being re-cut, and nothing is written into the device either way.

Both halves of a pin come from a package index: the versions and the hashes. Only
a version whose index entry records its `<archive>.meta.json` can be resolved
*through* — a package that does not say what it requires would leave the next
stage with nothing to go on — so a source that publishes packages without those
sidecars is refused by name rather than silently skipped.

Each entry overrides its own package and nothing else:

```yaml
sources:
  build_tools: build-tools/mcuhome-build-tools:0.1.0
```

An override is never refused for being outside what the chain declares: a device
may name another version, another package or another host, and the build says so
in one line and goes on —

```
Note: the device pins mcuhome-build-workspace 0.9.0 in sources.build_workspace,
and mcuhome-sdk 0.1.10 was built and tested with "~=0.1.0" — building with the
version the device names.
```

The stage above knows what it was tested with, not what is allowed.

The tools package is published per architecture and the bare family name is the
normal pin: it resolves to this host's package, and pinning the family still
pins every platform's bytes, which is what lets one build context produce the
same firmware on an amd64 host and on an arm64 one. Naming one platform's
package outright (`mcuhome-build-tools_linux-amd64`) is allowed and means exactly
that — a host of another architecture refuses rather than substituting something.

A reference may state a hash as well as a version:

```yaml
sources:
  build_workspace: build-workspace/mcuhome-build-workspace:0.1.0@sha256:3e63…
```

That decides the whole pin, and nothing is resolved for it — no chain, no index.
It is what an air-gapped machine states when it has the archives but no package
index for them. The stage below it still resolves as usual where a source
publishes exactly those bytes, because the pinned package's own meta file is
found beside them; where none does, that stage has to be stated too.

Everything else needs an index that lists the two packages, because a hash can
come from nowhere else: put them in one of the operator's own package
directories, or configure the registry.

### Which container image a container build runs in

A build context pins the build environment by its **packages**, never by an
image. A container image is one delivery of such a set: it declares the packages
it was assembled from as labels, and a container build looks for the image whose
labels are exactly the set the context pinned. An image assembled from the same
package versions but different bytes is a different environment and is never used
instead — the build is refused, with each candidate and the reason it was
rejected.

Where MCUHome looks is configuration, because it is a decision about trust:

```yaml
build:
  container_repositories:
    - ghcr.io/mcu-home/build-environment
```

The list is searched in order and the first repository holding a matching image
wins; unset, it is MCUHome's own repository. Where a repository holds several
images for one package set, the highest assembly revision (`…-r2` over `…-r1`)
is taken.

MCUHome publishes exactly one such image,
`ghcr.io/mcu-home/build-environment`, tagged
`<build workspace package version>-r<n>` — the workspace package it delivers,
plus a revision counter for a rebuild from the same packages. The tag is a
location and never the identity: what makes an image usable for a build is the
package set its labels declare, and two tags over one set are the same
environment.

A pin narrows the search for one build. It says *which* image to look at and
never that it may be run without being what it claims — the labels are checked
either way. Four forms, told apart by what the value starts with:

| pin | means |
|---|---|
| `ghcr.io/mcu-home/build-environment` | that repository, in place of the list |
| `:0.1.0-r2` | that tag, in the repositories of the list |
| `@sha256:…` | those bytes, in the repositories of the list |
| `ghcr.io/…/build-environment:0.1.0-r2` or `…@sha256:…` | exactly one image |

The leading `:` and `@` are what make a bare name unambiguous: written plainly
it is a repository.

Three places state a pin, and the more specific one wins. A device carries one
from build to build in `sources.container_image` — optional, written into a
device only by whoever wants it there; a configured builder carries one for the
machine it describes (`container_image:`); and a single build overrides both
(the command line's `--container-image`). All of them mean the same thing at
either target: a local container build resolves the pin against the repository
list above, and a remote build hands it to the server, which resolves it
against what its operator allows.

A build that starts no container has no image for any of them to name, and the
three are not answered alike, because they are not the same kind of statement:

- `--container-image` on a build in `subprocess` mode is **refused**. It is a
  statement about *this* build and cannot be quietly dropped; the refusal says
  to drop the image or to set `build.mode` back to `container`.
- a builder's `container_image:` and a device's `sources.container_image` are
  statements about a machine and about a delivery, so they produce **one line in
  the build log** — "the image has no effect here" — and the build carries on.
  Refusing the first would refuse every build on that machine, and refusing the
  second would refuse a device that builds correctly here and in a container
  elsewhere.
- a development build against a west workspace of your own is refused over the
  device's pin together with every other `sources.*` entry that differs from
  its default: that build fetches no packages at all.

The image runs with no network, as the calling user, and with exactly the tree
the build-environment specification defines mounted into it: the build context
and the SDK read-only, the output directory writable, and the compiler cache
tiers this machine provides. One fresh container per step, thrown away when the
step ends.

### What one build may use of this machine

A build is given a CPU and a memory budget. It travels two ways at once:
into the build environment, which sizes its parallelism from it, and — in a
container build — onto the container itself, which the runtime holds to it. The
second exists because the first is a recommendation: a build environment may
have a bug, and a machine should survive it.

```yaml
build:
  cpus: 6            # cores; fractional is allowed, as in `docker run --cpus`
  memory: 12g        # 512m, 8g, or a plain byte count
```

Unset means the machine as it is: every core, and — where the machine can be
measured, which today means a Linux host — the memory that is actually free.
Where it cannot, the memory budget is left unstated rather than guessed at, and
the build environment sizes itself. A container build additionally caps the
number of processes in the container — nothing that compiles firmware comes near
that bound, and a build that does is not compiling.

A build without a container states the same budget and enforces nothing: there
is no container to hold it to a figure. Where a build has to be held to one,
build it in a container.

### Building without a container

A local build runs in a build container by default. The other way is to run the
build environment directly on this machine, as an ordinary child process:

```yaml
build:
  mode: subprocess          # container (the default) | subprocess
```

It is for machines where a container runtime is unavailable — inside an
unprivileged container, on a locked-down host — and for builds you already
trust. **It isolates nothing.** The build runs with your own rights, and a build
context is untrusted input: it carries patches, and patches are code. Build
somebody else's context in a container, and never offer this mode to strangers;
a build server does not.

What it needs is a host that qualifies (below) and the environment's packages,
which MCUHome fetches, verifies and unpacks itself the first time. After that a
build needs no network at all. It runs the same packages a container build
runs — the image is an assembly of exactly them — so the two ways compile one
context against the same bytes. Pinning an image for *this* build
(`--container-image`) is refused here rather than half-honoured; a pin that
came with the device or with the machine's builder is noted in the log and
changes nothing (above).

The compiler cache follows the same layout a container build uses, so a machine
that built both ways has one cache. Each tier can be moved on its own:

| key | what it is |
|---|---|
| `build.cache_root` | where this machine keeps the cache; unset it is the user's cache directory |
| `build.cache_local` | this machine's own cache; unset it lives under `build.cache_root` |
| `build.cache_shared` | a cache shared with other machines, offered read-only; the directory has to exist |
| `build.cache_session` | kept for one build session; unset there is no session tier |
| `build.cache_project` | kept for one project; unset there is no project tier |

### The host a build without a container needs

The container is simply a host that always qualifies; without it, this machine
has to. The compiler toolchain, cmake, ninja, west and gn come with the build
tools package, so what the host itself has to provide is short — and one line of
it is not a floor but an exact version:

| what | requirement | why |
|---|---|---|
| operating system | Linux on x86_64 or aarch64 | every prebuilt tool in the package |
| C library | glibc ≥ 2.28, no musl | the Zephyr SDK toolchain |
| Python | exactly the minor the build tools package was built with — the current Debian stable's, 3.13 today | the compiled wheels in that package |
| git | any current version | west and the build's version stamping shell out to it |

**Why Python is exact.** The tools package carries the Python packages a build
needs as wheels, and some of them are compiled against one version of Python.
Compiled wheels install into that minor and no other, so MCUHome checks the
interpreter before it creates anything and refuses with the version it needs;
nothing is downloaded or compiled to paper over the difference. If the machine's
`python3` is another minor, name one that is not:

```yaml
build:
  python: python3.13        # a command on PATH, or a full path
```

Or build in a container, where the question does not arise.

### The build environment store

A build that does not run in a container needs its build environment as files on
this machine. The workbench unpacks the environment's packages into a store under
the user's own cache home:

```
${XDG_CACHE_HOME:-~/.cache}/mcuhome/build-environments/<package name>-<version>/
```

Somewhere else if you say so — a volume with room for it, or off a network home
directory:

```yaml
build:
  env_store: /srv/mcuhome/build-environments
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
directory. The host's Python decides whether an entry can be finalized at all,
which is the section above.

**Where the packages come from, and how large they may get.** They are looked
for in the operator's own directories first and on the registry second, like
every other package. The environment packages are two orders of magnitude
larger than the SDK, so each may be kept somewhere else, and each unpacks under
a bound of its own — a bound is protection against an archive that expands
without end, not a budget, and the defaults are an order of magnitude above what
MCUHome's own packages need:

| key | default |
|---|---|
| `build.workspace_sources`, `build.tools_sources` | none — each kind is looked for under its own key, and the registry answers where no directory does |
| `build.sdk_max_bytes` | 2 GiB |
| `build.workspace_max_bytes` | 20 GiB |
| `build.tools_max_bytes` | 10 GiB |

Raise a bound for a package of your own that is legitimately larger; a package
whose contents exceed it is refused and leaves nothing behind.

**Filling it without building.** A build fills the store on its way past, and
that is not always when you want it filled: a CI job has just produced a package
and wants it unpacked the way a build would unpack it, a laptop is about to go
somewhere without a network. `provision_environment` is that step on its own —
the same acquiring, unpacking, finalizing and freezing, for one package named
either as a file or by name:

```python
from mcuhome.workbench import api

options = api.resolve_build_options(settings)
entry = api.provision_environment("mcuhome-build-workspace", options=options, env=env)
entry = api.provision_environment(
    Path("mcuhome-build-tools_linux-amd64-0.2.0.tar.zst"), options=options, env=env
)
```

A name is resolved against the directories above and the registry, narrowed with
a constraint (`mcuhome-build-workspace:~=0.2`) where a particular one is wanted.
A file is taken as it is and identified by the hash computed from it, because
there is no pin to check it against — which is what makes it the form for a
package that was built a minute ago and is published nowhere yet. Either way the
answer is the store entry, and a package that is already there is answered
without unpacking it a second time.

### Building against a west workspace of your own

If you are working on the SDK itself — or on the sources the build environment
carries — point the build at your own west workspace instead of at a
provisioned environment. This is the supported way to build what you are
editing; nothing else takes a working tree as its build environment.

```yaml
build:
  mode: subprocess
  dev_workspace: ~/work/mcuhome-workspace
```

That one path names the **whole** environment. The workspace carries the
sources, its manifest repository is the SDK that gets compiled, and the tools
are the ones on the `PATH` the build was started from — your west, your CMake,
your Zephyr SDK, your `ccache` configuration. Nothing is fetched, nothing is
unpacked, nothing is finalized, and nothing is verified: MCUHome checks that
the directory is a west workspace with its manifest repository checked out, and
nothing else. Those bytes are yours.

**Which directory to name.** The workspace, not the SDK checkout — the
directory `west init -l` anchored, holding `.west/`, `zephyr/`, `modules/`,
`bootloader/` and the `mcuhome-sdk` checkout beside them:

```
mcuhome-workspace/          <- build.dev_workspace names this
  .west/
  mcuhome-sdk/              the manifest repository: the SDK this build compiles
  zephyr/  modules/  bootloader/
```

The checkout has to lie **inside** the workspace. `west init -l` resolves a
symlinked manifest repository and anchors the workspace at the checkout's
physical parent, so a checkout somewhere else with a link into the workspace
gives a workspace west does not recognise; the link goes the other way round —
put it wherever you are used to reaching the repository at, and point
`build.dev_workspace` at the real directory. The SDK repository's README has
the recipe.

The build runs the way every other build runs — the same per-step directories
under the build directory, the same request document, the same view of the
workspace under `work` — with two differences. The builder is started as
`python3 -m mcuhome.compiler.abi` out of your own checkout rather than through
the packaged entry point, because the entry point's whole job is to set up an
environment you already have. And **MCUHome writes nothing into your
workspace**: what the builder needs and a checkout does not carry — the record
of where its layers are — is written into the build directory and points at
your workspace from outside it. Neither the workbench nor the builder creates,
moves or edits anything in there; the build directory holds the generated
application, the build tree and the artifacts.

One qualification, because it is the difference between a promise and a
half-promise: the view the build compiles in reaches your files through **hard
links**, so a tool the build drives that wrote to a source file in place would
write through to your working tree — exactly as it would in a `west build` you
ran yourself. Measured over a full real build of the reference device: every
file in the workspace came out with the same content, the same mode and the
same timestamp, and one directory did not — `modules/lib/openthread/.git`,
because Zephyr's version stamping runs `git` in the projects it builds. That
is the shape of what to expect here: MCUHome writes nothing, and the tools a
Zephyr build runs are the tools a Zephyr build runs.

What is refused rather than half-done:

- a build context that carries a **patch**, because your workspace is yours and
  a build that quietly patched it — or quietly ignored the patch — would be
  wrong either way;
- a device that states a `sources.*` entry that differs from its default: the
  package references name something to fetch and this build fetches nothing, and
  `sources.container_image` — empty by default, so any stated value differs —
  names an image for a build that starts no container;
- `build.mode: container` together with `build.dev_workspace`, because a
  container has neither your workspace nor your tools;
- a directory that is not a west workspace, or whose manifest repository is not
  checked out — that repository is the SDK this build compiles;
- a workspace `west` itself cannot read, or one whose manifest is not MCUHome's:
  the build asks `west list` where the Zephyr, MCUboot and Matter trees are, and
  says so when the answer does not have them;
- an already-created context that pins a build environment, handed to a
  development build — the firmware would then carry a context claiming packages
  it never saw.

The build context such a build writes says `build_environment: developer` and
pins no SDK package. That is honest and it has two consequences: the context is
**not reproducible** — its identity covers the files and the board, never the
bytes it was compiled against — and it is **not remote-buildable**, so sending
it to a build server is refused before the upload.

## Secrets

Everything a project must not commit lives in its `secrets/` directory, one file
per kind and name: `secrets/main.yaml` for the values every device shares,
`secrets/device/<device>.yaml` for a device's own, `secrets/builder/<name>.yaml`
for a build server's token, and `secrets/signing/key.pem` — the firmware
signing key — with the `secrets/signing/key.yaml` that references it. A device
configuration reads a value with `!secret <name>`, its own file first and the
shared one second.

Six calls are the supported way to look at those files, and **no document any of
them answers carries a value**:

```python
for scope in api.find_secret_scopes(project):
    print(scope.kind, scope.name, scope.exists)

shared = api.read_secrets(project, kind="main")
for entry in shared.keys:
    print(entry.key, entry.masked, entry.used_by)  # masked is a constant

api.set_secret(project, kind="main", key="wifi_password", value="…")
api.unset_secret(project, kind="device", name="thermostat", key="passcode")
api.delete_secret_file(project, kind="builder", name="attic")
```

`read_secrets` masks every value with the same constant — a mask that kept the
length or the first character would be part of the secret — and says for the
shared file which devices actually read each entry, following the same ladder a
build follows. `reveal_secret(project, kind=…, key=…)` is the one call that
answers a value, and it has a verb of its own so that it cannot be made by
accident.

Writing is a round trip: comments, order and everything else in the file survive
`set_secret`, the first secret of a scope creates the file with mode 0600, and
removing the last one leaves an empty file rather than a deleted one.
`delete_secret_file` removes a whole device or builder file and refuses for the
project's own two, which are emptied entry by entry instead. Every one of the
six refuses a secrets file other users can reach, naming the `chmod` that fixes
it, rather than reading it — and none of them ever prints key material: the
signing key is a file, drawn by `create_signing_key`, and `set_secret` refuses
that scope.

## Security

Firmware is signed on the machine the user controls, never on a build server:
what travels to a builder is the build context and the public half of the key.
The key is drawn per project and kept in the project's `secrets/signing/`
directory — `key.pem` with the `key.yaml` that references it — or at a path an
embedding application states; MCUboot's published demo key is never
used for a signature. Drawing one is its own call: reading a project's key never
creates one, so a client that only shows the public key cannot make a project a
second vendor by looking at it. Report a vulnerability as described in the organization's
[security policy](https://github.com/mcu-home/.github/blob/main/SECURITY.md).

## Documentation

- [`docs/api.md`](docs/api.md) — the reference of the programmatic surface:
  every exported name, every option with its variable and flag, every file and
  every document shape
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
