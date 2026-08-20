# 0028 — One naming scheme for repositories, distributions and import paths

- Status: draft
- Date: 2026-08-20

Product-owner decision round of 2026-08-20. It renames repositories,
Python distributions, import paths and two container images. It changes
no protocol, no file format, no invocation ABI and no behaviour; every
gate that was green before is green after.

It supersedes the naming halves of ADR 0020 (decisions 1 and 2) and
ADR 0024, which named the repositories as they stood at the split.

## Context

The names grew one decision at a time and stopped agreeing with each
other. Four repositories carried a generic noun (`cli`, `dashboard`,
`build-server`, `ha-apps-repository`) while three carried the product
name and two carried a domain. The seven Python distributions split
three ways: three already lived in the shared `mcuhome` namespace
(`mcuhome.model`, `mcuhome.compiler`, `mcuhome.workbench`), four did
not (`mcuhome_cli`, `mcuhome_dashboard`, `mcuhome_buildserver`,
`mcuhome_packages`) — so `pip install`ing two MCUHome packages gave you
two unrelated-looking top-level modules. One distribution (`mcuhome-ui`,
then `mcuhome-dashboard`) had a fourth name again in its container
images.

None of this broke anything. All of it cost a reader time, and every new
component had to guess which of the four conventions applied.

## Decision

**1. Every repository is named after what it ships**, with the product
name in front where the thing is a MCUHome component:

| was | is |
|---|---|
| `mcuhome` | `mcuhome-workbench` |
| `cli` | `mcuhome-cli` |
| `dashboard` | `mcuhome-ui` |
| `build-server` | `mcuhome-buildserver` |
| `packages.mcuhome.org` | `mcuhome-packagetool` |
| `t.mcuhome.org` | `site-t.mcuhome.org` |
| `ha-apps-repository` | `homeassistant-apps` |

`mcuhome-sdk` was already right.

**A repository that ships a distribution is named after it.** That is
why the package host became `mcuhome-packagetool` rather than keeping
its domain: it holds `mcuhome.packagetool`, and that it also has a
workflow publishing a GitHub Pages site does not decide the name. The
`site-` prefix is left for the case where a served site is *all* there
is — today only `site-t.mcuhome.org`, which contains nothing but HTML.

**2. Every distribution imports from the one `mcuhome` namespace.**

| distribution | imports as | ships the |
|---|---|---|
| `mcuhome-model` | `mcuhome.model` | vocabulary, dependency-free |
| `mcuhome-compiler` | `mcuhome.compiler` | code generation, stages 4-5 |
| `mcuhome-workbench` | `mcuhome.workbench` | stages 1-3, build methods, signing |
| `mcuhome-cli` | `mcuhome.cli` | the `mcuhome` command |
| `mcuhome-ui` | `mcuhome.ui` | the web interface |
| `mcuhome-buildserver` | `mcuhome.buildserver` | the build server |
| `mcuhome-packagetool` | `mcuhome.packagetool` | the package host's publishing tool |

`mcuhome` stays a PEP 420 namespace: no distribution ships
`mcuhome/__init__.py`, and a test asserts it of every built wheel.

**3. The CLI distribution is `mcuhome-cli`, not `mcuhome`.** This
reverses ADR 0020 decision 2, which reserved the plain name so that
`pip install mcuhome` would yield the command. The reservation bought
one convenience and cost the scheme its only exception; nothing is on
PyPI yet, so it cost nothing to undo. The *command* is still `mcuhome`
— that never depended on the distribution's name. Whether `mcuhome`
later becomes a meta-distribution that depends on `mcuhome-cli` is left
open until the PyPI names are reserved.

**4. A hyphen separates words where a hyphen is legal; elsewhere the
words run together.** A distribution name may carry hyphens and does
(`mcuhome-buildserver`); a Python identifier may not, so the module
concatenates (`mcuhome.buildserver`). This is not two conventions but
one, applied to two alphabets — and the concatenated form is already
the house style for module names (`buildimage`, `buildlock`,
`contextstore`, `sessionclient`, and twenty more).

**`buildserver` is one word on both sides**, because in this project it
is a product — *the MCUHome Buildserver* — and not the English phrase
"a build server". Prose still uses "build server" for the *role*, which
is the distinction ADR 0019 draws and this rename does not touch.

**5. The container images follow the distribution**, minus the
redundant vendor prefix a registry path already supplies:

| was | is |
|---|---|
| `ghcr.io/mcu-home/mcuhome-dashboard` | `ghcr.io/mcu-home/ui` |
| `ghcr.io/mcu-home/mcuhome-dashboard-homeassistant` | `ghcr.io/mcu-home/ui-homeassistant-app` |

`ghcr.io/mcu-home/build-container` is unchanged: it is not a
distribution and never carried the prefix.

## Consequences

**Nothing outward breaks on the rename itself.** GitHub answers the old
repository names with a 301 and resolves them in both the REST API and
`git`, so an old clone URL, an old `actions/checkout`, and an old
`pip install git+…` all keep working. Every reference was rewritten
anyway — 273 of them across eight repositories — because a redirect is
a grace period, not a design.

**Two GitHub Pages sites kept their custom domains.** `packages.
mcuhome.org` and `t.mcuhome.org` are served from renamed repositories
and were verified live afterwards. The domain never depended on the
repository name; the committed `CNAME` is what holds it.

**The container images must be published before the app metadata moves.**
A GHCR package cannot be renamed — the new names come into existence
with the next release. So `homeassistant-apps` may only raise its
`image:` and `version:` after that release exists, which is the order
ADR 0018 already prescribes for a version bump.

**The signing key, the build context, the session protocol and the
build-container contract are untouched.** So is every generated
artifact: a device built through the renamed packages produces the same
firmware, and the end-to-end build was run to confirm it rather than
assumed.

## What this does not do

Prose that names a *component* rather than a repository — "the
dashboard", "the CLI", "a build server" — is left alone. Historical
statements in finalized ADRs and changelogs keep the names that were
true when they were written; git history is not rewritten to match.
