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

## Open points

- Builder-level defaults beyond type options (preferred image, jobs) —
  deliberately not included yet.
- The exact YAML keys of `secrets/build-server/<name>.yaml` (today:
  `token`) are pinned during implementation.
- ~~§4's user/system-level `secrets/build-server/` directories~~ —
  confirmed by the product owner (2026-08-14): intended exactly so.
