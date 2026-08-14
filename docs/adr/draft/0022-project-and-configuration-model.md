# 0022 — The project directory and the configuration model

- Status: draft
- Date: 2026-08-14

Product-owner design round of 2026-08-14. Platform-owned on purpose:
the CLI and, later, the dashboard resolve configuration through the
same module — same machine, same user, same project ⇒ same behavior,
whichever tool asks.

## Context

The user-facing tools grew ad-hoc configuration: a tree root
auto-discovered from the cwd, one TOML file for build servers, a
handful of `MCUHOME_*` variables read in different places, and no way
to set most options outside the command line. When the dashboard later
changes a default, today's CLI would not notice — the two tools have
no shared notion of "the configuration".

## Decision

### 1. The project directory

A user's work lives in a **project directory**: the folder holding the
**project marker** `.mcuhome-project-root` (PO 2026-08-14) — a
dedicated dotfile whose only content is a comment line saying what it
is. The marker is deliberately not a configuration file: a
`mcuhome.yaml` can plausibly lie around in folders that are no project
root (a copied example, a config snippet), and only a file that exists
for exactly one purpose can never mark one by accident. Marker =
identity, `mcuhome.yaml` = project-level **configuration**, which is
therefore *optional* — a project without one simply has an empty
project layer.

Resolution: start at the cwd and search **upward** (git-like) for the
marker. An explicit `--project-dir PATH` (or `MCUHOME_PROJECT_DIR` as
its environment fallback) disables the search and is an error if the
named directory carries no marker.

Project layout:

```
.mcuhome-project-root     # the marker (one comment line, no config)
mcuhome.yaml              # project configuration (optional)
devices/<name>/main.yaml  # one folder per device
secrets/                  # ALL secrets, no exceptions (mode 700)
  main.yaml               #   project-wide secrets (the former secrets.yaml)
  devices/<name>.yaml     #   per-device secrets (future)
  build-server/<name>.yaml #  per-builder credentials (ADR 0023)
  firmware/mcuboot.yaml   #   MCUboot signing key (`firmware_signing_key`, draft 0015 §8)
build/                    # build output (disposable, created by builds)
.gitignore                # contains secrets/
```

`mcuhome init` creates the durable part of this — the marker,
`mcuhome.yaml`, `devices/`, `secrets/` (mode 700) and the
`.gitignore` — after checking the target directory: a non-empty
directory draws a warning and a refusal, `--force` proceeds anyway
(and may overwrite files). Pinned during implementation: the
`.gitignore` covers `secrets/` **and** `build/` (disposable output is
the first accidental commit of every new project); under `--force` an
existing `mcuhome.yaml` is left alone — it is the user's configuration
— while the marker is completed and missing ignore lines are appended
rather than the file rewritten. Creating a *project* is init's job
alone: `device new` refuses outside a project (the old zero-ceremony
"a devices/ folder makes a tree" rule retired with the markers that
carried it), and a bare YAML file validated outside any project gets
its own directory as a stand-in root, with `secrets/main.yaml` next to
it.

### 2. Five configuration layers, strict precedence

Ascending — later wins:

| Layer | Where | Via |
|---|---|---|
| system | platformdirs site config (Linux `/etc/mcuhome/`, Windows `ProgramData\mcuhome\`) `configuration.yaml` | file |
| user | platformdirs user config (Linux `$XDG_CONFIG_HOME/mcuhome/`, Windows `%APPDATA%\mcuhome\`) `configuration.yaml` | file |
| project | `mcuhome.yaml` in the project directory | file |
| environment | `MCUHOME_*` variables | env |
| command | the invocation's arguments | args |

Two deliberate bootstrap exceptions, and they run **first** (PO
2026-08-14): `--project-dir` and, as its fallback,
`MCUHOME_PROJECT_DIR` are evaluated before any layer is read — they
decide where the project layer even is, so they stand outside the
five-layer merge and can never themselves be set from a configuration
file.

The system/user files are deliberately **not** named `mcuhome.yaml`,
and no directory outside a project carries the marker: only a project
directory may look like a project directory — a config directory must
never be mistaken for one by the upward search or by a user working
inside it (PO 2026-08-14).

The directories follow the **platformdirs conventions, not the
platformdirs library**: that library answers out of the process
environment, and the workbench serves several sessions from one
process (ADR 0020's stated-environment invariant), so the paths are
computed from the environment the caller states. A layer whose
directory the stated environment cannot name — no `HOME`, no
`XDG_CONFIG_HOME`, no `%APPDATA%` — is simply an absent layer, not an
error: a service account reading only project configuration is a
normal caller. Relative paths written in a configuration file resolve
against that file's directory (the file cannot know where the reading
process stands; its author can see its own neighborhood).

### 3. Schema-driven, with per-option channels

Every option is declared exactly once — name, type, default, and
**which channels may set it** — and the flag spelling, the `MCUHOME_*`
name and the config key derive from that declaration. Not every option
belongs in every channel: a per-invocation value (the device to build)
is argument+environment only and never lives in a static file; an
environment-shaped value (the default builder) is settable through all
five. The declaration is the single source; "settable everywhere it
makes sense" holds by construction, and `mcuhome config print` (the
resolved tree, each value with its origin layer) falls out of the same
registry.

Pinned during implementation: the registry is
`mcuhome.workbench.configuration.OPTIONS`; channels are three switches
per option — files (all three file layers as one), environment,
arguments — plus the bootstrap mark, and a file that sets an option
outside its channels is refused with the channel rule in the message.
The platform options of this draft's scope are `sdk_sources` (all
channels; the canonical spelling is **plural**, `MCUHOME_SDK_SOURCES`,
`PATH`-style separated — the old singular `MCUHOME_SDK_SOURCE` retires
with the CLI's C2 migration), `signing_key` (argument+environment
only, ADR 0015 §8's override) and `jobs` (all channels); ADR 0023 adds
`builders` and `default_builder`. List-valued options list in files,
split `PATH`-style in the environment. Tools may run additional
registries of their own (the CLI's presentation options) through the
same machinery; the platform registry stays the shared one.

### 4. Ownership: an explicitly-invoked workbench module

The model is implemented once, as a workbench module the tools call
**explicitly** (resolve → explicit values → `api`).
`mcuhome.workbench.api` itself keeps taking explicit inputs only — it
never reads environment variables or configuration files, so embedders
and third parties see no hidden magic. The dashboard adopts the same
module when its turn comes; until then this module is the contract.

### 5. Secrets hygiene

`secrets/` is created mode 700, files in it 600. Every reader checks
permissions: insecure permissions draw a warning, and for key material
(signing keys, future Matter/attestation keys) the tools **refuse**.
`mcuhome init` writes the `.gitignore` line so a project can be
committed without ever committing its secrets.

Pinned during implementation: the check is per file (a 600 file is
protected whatever its directory says), runs in every reader — the
`!secret` loader and the signing-key reader today — and warnings
travel through a caller-supplied `on_warning` callback, because the
workbench prints nothing.

**The `!file` tag** (second PO round 2026-08-14): any YAML the
workbench loader reads may make a value out of an external file —
`key: !file name.pem` — and the mechanism is deliberately generic, for
every current and future value an external tool wants *as a file*.
The resolved value is the file's raw content (a `str` to every
consumer, so it composes with existing readers unchanged) and carries
the file as `value.path`, always the real absolute path (symlinks
resolved), ready for a tool boundary like `imgtool --key <file>`.
Rules, pinned: a relative reference resolves against the referencing
YAML file's directory (this section's path rule); `~` is refused with
the reason (a configuration file answers for itself); resolution is
eager and strict — a reference to a file that does not exist or cannot
be read stops the load at once, located at the tag's line; the tag is
**not** `!include`, because in the Home Assistant world that means
"parse and inline YAML" and this means "the bytes of that file,
verbatim". A consumer that treats such a value as a secret extends its
permission check to `value.path` (the signing key refuses, the builder
token warns — same ladder as the file the reference stands in). Writers
that round-trip a loaded document use the loader's `editing_yaml`, which
writes a reference back as the reference — never as the content.

The founding consumer is the signing key: `secrets/firmware/mcuboot.pem`
referenced from `mcuboot.yaml`, imgtool takes the path directly, and
the short-lived key materialization this paragraph used to describe is
gone with the inline PEM form (ADR 0015 §8).

## Consequences

- The CLI's `--config-root` and the cwd-upward *tree* discovery are
  superseded by the project directory; `devices/<name>/main.yaml`
  keeps its shape, so device configurations move unchanged.
- The former tree-root `secrets.yaml` becomes `secrets/main.yaml`;
  the `!secret` mechanism (yaml-schema.md §9) follows — a platform
  change tracked with this draft.
- Tool symmetry becomes an implementation fact rather than a promise:
  when the dashboard later changes the default builder, the next
  `mcuhome device build` uses it.
- The CLI bindings (flag spellings, scope flags) live in cli ADR 0003;
  this draft owns the semantics.

## Open points

- ~~The workbench module's name~~ — pinned with the implementation:
  two modules, `mcuhome.workbench.project` (marker, layout, init,
  secrets hygiene) and `mcuhome.workbench.configuration` (registry and
  layers), both surfaced through `mcuhome.workbench.api`. Whether they
  later become their own distribution for third-party tools stays
  open.
- Merge/list semantics for structured values other than builders
  (builders are defined in ADR 0023); scalars are simply
  nearest-wins.
- ~~The system/user file name~~ — decided (PO 2026-08-14):
  `configuration.yaml`, rationale in §2.
- ~~The signing-key location~~ — decided (PO 2026-08-14): per project,
  `secrets/firmware/mcuboot.yaml` under `firmware_signing_key`
  (draft 0015 §8 updated).
