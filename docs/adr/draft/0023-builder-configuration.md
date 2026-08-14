# 0023 — Builder configuration

- Status: draft
- Date: 2026-08-14

Product-owner design round of 2026-08-14. Platform-owned: the CLI and
the dashboard select builds through the same configuration (ADR 0022);
the CLI's flag spellings live in cli ADR 0003.

## Context

Where a build runs is deployment configuration, and today it is spread
over three spellings (`--method`, `MCUHOME_BUILD_METHOD`,
`build-servers.toml` + `tokens/<label>`) that only the CLI knows. The
product goal is the opposite: `mcuhome device build` should simply
work, and *where* it built is something the user configured once —
in the HA world (two Apps, builds always remote) typically never,
because the dashboard App ships the right default.

## Decision

### 1. Named builders

Configuration (any layer of ADR 0022) declares a list of **builders**:
each has a `name`, a `type`, and type-specific options.

| Type | Options |
|---|---|
| `local-dev` | path to the west workspace (host toolchain builds) |
| `remote` | server address (IP/hostname[:port]); credentials come from `secrets/build-server/<name>.yaml` |
| `local` | none required — local docker, default build-container image; socket path and image optionally configurable |

`default_builder` names the builder a plain `mcuhome device build`
uses.

### 2. Selection

Three rungs, most explicit wins: fully manual
(`--build-mode` plus its mode-specific flags — server address, token,
workspace path — bypassing the builder list entirely), a named builder
(`--builder NAME`), or the configured `default_builder`. The method
vocabulary underneath (`local`/`local-dev`/`remote`), its validation
and its typed refusals stay the workbench's (ADR 0020); a builder is
configuration *about* a method, never a fourth method.

### 3. Merge semantics across layers

Builder lists merge **by name** across the configuration layers; on a
name collision the layer nearer the project wins whole (no per-field
merging of one builder from two layers). `default_builder` is a
scalar, nearest wins. So a machine can ship site builders in the
system layer, a user can add their own, and a project can pin its
default — without any layer having to repeat the others.

### 4. Per-builder credentials

Each remote builder's credentials live in
`secrets/build-server/<name>.yaml` (project layer; the same relative
path exists under the user and system config directories for builders
defined there — nearest wins). YAML on purpose: today it carries the
bearer token, later it can grow certificate/TLS-pinning material when
the session protocol does — one file per builder, whatever the
credential shape becomes. File hygiene per ADR 0022 §5 (600, checked,
refuse on insecure key material). Channel security itself
(TLS, mutual authentication) is session-protocol work on the
build-server side, deliberately not designed here; the tools' duty is
carrying credentials to `mcuhome.workbench.api` explicitly.

### 5. Retirements

`build-servers.toml`, `tokens/<label>`, `MCUHOME_BUILD_METHOD`,
`MCUHOME_BUILD_SERVER`, `MCUHOME_BUILD_TOKEN` and the CLI spellings
`--method`/`--server`/`--token` retire without aliases (pre-1.0, the
E62 rule). The workbench refusal hints that quote spellings follow the new
vocabulary (`mcuhome/workbench/buildmethods.py` names the retired
spellings in its hint texts — the retirement touches those too).

## Consequences

- The default experience matches the product: an HA user never sees
  any of this (the dashboard App configures a remote builder as
  default); a standalone user configures once, or types the manual
  rung like today.
- Today's default stays `local`; the expected future default is
  `remote` — a configuration default, changeable without touching the
  vocabulary.
- The dashboard's build-method deployment setting (dashboard ADR 0013)
  becomes a consumer of this model when the dashboard adopts ADR 0022.

## Pinned during implementation (2026-08-14)

- The model lives in `mcuhome.workbench.builders` (vocabulary: parse,
  merge-by-name, selection) and
  `mcuhome.workbench.configuration.resolve_builder` (the layer- and
  secrets-aware entry point the tools call); `builders` and
  `default_builder` are ordinary options of ADR 0022's registry.
- Channels: `builders` is **file-layers only** — deployment
  configuration; the per-invocation channel is the manual rung.
  `default_builder` is settable up to the environment
  (`MCUHOME_DEFAULT_BUILDER`); the *invocation* selects with
  `--builder`, which is selection rather than configuration, so the
  option's own arguments channel is off. (This sharpens §2's "settable
  through all five" example in ADR 0022 §3 for this one option.)
- A builder's name becomes the credentials file name, so it is
  restricted like a device name: lowercase, digits, dashes.
- `local` accepts an optional `image`; `local-dev` requires
  `workspace` (`~` and relative paths resolve per ADR 0022's file
  rule); `remote` requires `server`. A `token` key in the builder list
  itself is refused toward the secrets file.
- `secrets/build-server/<name>.yaml` carries `token` (a string).
  Unknown keys there are tolerated — future TLS/certificate material,
  not typos. The **nearest existing file answers whole** (a project
  file without a `token` key means "no token", it does not fall
  through to the user's); no file anywhere is a tokenless builder,
  which stays permitted. File hygiene per ADR 0022 §5: a warning for
  an exposed token file, refusal is reserved for key material.
- `config print` shows each merged builder with the layer that defined
  it — merge-by-name makes origin a per-builder fact.
- §5's retirements are done in the workbench's own texts (the
  `buildmethods` refusals now speak this ADR's vocabulary); the CLI's
  spellings retired with the vocabulary step (cli ADR 0003, C2,
  2026-08-14) — `--build-mode`/`--builder`/`--build-server`/
  `--build-token` are live, `build-servers.toml`, `tokens/<label>` and
  `MCUHOME_BUILD_*` are gone.
- The manual rung's flag pairing is the CLI's validate phase (cli ADR
  0003's C2 pins): mode flags without `--build-mode`, mixed rungs, or
  `remote` without a server are exit-2 refusals before anything runs.
- A `local-dev` builder's `workspace` (or the manual `--workspace`)
  becomes the *only* discovery start for the west workspace — the
  install-location/working-directory discovery serves only the
  unconfigured case.

## Open points

- Builder-level defaults beyond type options (preferred image, jobs) —
  deliberately not included yet.
