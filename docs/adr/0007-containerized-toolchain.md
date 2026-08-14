# 0007 — Containerized toolchain, minimal host requirements

- Status: accepted
- Date: 2026-08-02
- Finalized: 2026-08-14

## Context

Zephyr development normally requires a locally installed Zephyr SDK
(cross-compilers for every target architecture), west, Python tooling and
per-vendor flash utilities — a heavy, error-prone host setup. MCUHome must
additionally build firmware under Home Assistant, where the build
environment lives inside a container anyway. The Zephyr project publishes
official CI/build container images (`ghcr.io/zephyrproject-rtos/ci`) that
bundle the SDK — proof that the toolchain containerizes cleanly. MCUHome's
own image is nevertheless built from a plain Debian base with exactly the
tools a build needs and nothing else;
[`containers/builder/README.md`](../../containers/builder/README.md)
lists what is in it and, deliberately, what is not.

## Decision

Product-owner requirement: keep host prerequisites minimal.

**On the developer machine: git and docker. That is the whole list** for
a compiling build — no Zephyr SDK, no cross-compilers, no vendor flash
tools on the host. The `mcuhome` command itself adds nothing
toolchain-shaped — a standing constraint of the shell, recorded on its
side (cli ADR 0002); `mcuhome validate` and `mcuhome build
--generate-only` need Python and nothing else.

**In the build container: everything that compiles.** The Zephyr SDK and
toolchain, west and Zephyr's Python requirements, the Matter SDK's build
tools (`gn`, `zap-cli`), ccache. Not flash utilities — the original decision listed them among
the container's contents, but the image deliberately ships none: USB
passthrough
into containers is fiddly, especially under Home Assistant OS, so device
flashing will prefer browser-based flashing (WebSerial/WebUSB, as
popularized by ESP Web Tools) and OTA updates over container-side USB
access. This ADR anticipated that direction and deferred the details as
a design-phase topic; the design phase has since produced
[draft ADR 0016](draft/0016-device-onboarding-and-flash-transport.md),
whose constraint is this ADR's own — a compiling, flashable MCUHome must
not grow host prerequisites beyond git and docker.

**The build container is the build environment, and it is a replaceable
part.** Any container satisfying the published contract
([build-container-contract.md](../design/build-container-contract.md))
is a usable build container, third-party ones with their own toolchains
included, and it is driven through a frozen invocation ABI rather than
by knowing what is inside it (ADR 0019 decision 4). MCUHome's own image
(`containers/builder/`, published as `ghcr.io/mcu-home/builder`) is the
reference implementation of that contract and one conforming build
environment among possibly several. The contract also bounds what "your
own" may mean: a conforming build container must execute MCUHome's code
generation out of the SDK tree it is handed, so "bring your own build
container" is own toolchain and own Zephyr, never own build logic
(contract §6.1). Originally this ADR made the MCUHome image "the single
build environment, used identically by developers, CI, and the Home
Assistant add-on"; the remote-build design of 2026-08-08 replaced that
formulation when it became clear the same principle — the environment is
the container, the driver stays outside it — holds without MCUHome's
image being privileged, provided the interface is a contract rather than
shared knowledge.

**Driving happens only through the contract.** A backend writes the
request document, invokes the container's program (`/mcuhome/run
<action> <request>` in the reference image) and reads the result
document back; a backend that assembles a build command on the host and
hands it into the container is generating MCUHome firmware outside the
one path the contract defines. The container path is the normal path —
CI's Matter job compiles the reference device through the full contract
path on the unchanged published image — and `--method local-dev`, which
compiles on the host toolchain, is the contributors' escape hatch, not a
second supported product path.

**The terms are kept apart deliberately.** A **build container** is the
build environment and never a service; a **build server** "orchestrates;
it is never a build environment" (ADR 0020 decision 4) — which is what
keeps it able to drive build containers it did not build. Where no
container runtime is available — the Home Assistant App case — the
contract's `subprocess` backend profile (§1.2) applies: the build
environment is the one the server itself runs in, the program is still a
separate process, and the ABI is identical. Older documents call the
reference image the "builder image"; it is the build container /
build-container image throughout the newer ones.

**Versioning is lockstep; identity is the digest.** Container images are
versioned in lockstep with the Zephyr pin in `west.yml`: the tag is
`zephyr-<release>-r<revision>` (today `zephyr-4.4.0-r7`), a new `r`
revision for a change at the same Zephyr release, a new release segment
with every Zephyr bump (ADR 0008). A test reads the pin out of
`west.yml` rather than restating it, because a lockstep rule nobody
checks holds until the first bump. Since r3 the image also bakes a west
workspace of its own at `/mcuhome/workspace`, at the revisions `west.yml`
pins — `git describe` there decides `BUILD_VERSION` and therefore the
firmware bytes, so baking it makes that state a property of the image
digest. The lockstep is a release-process rule, not an identity rule: a
backend names a chosen image by its digest and never by its tag
*wherever the image has a repo digest*, while `container.image` and
`container.tag` are the human-readable trail beside it. An image built
on the host and never pushed has no repo digest; it is served, named by
the tag its host lists it under, and recorded `digest: null` (contract
§3.2) — the honest answer for bytes nobody can fetch, and a window the
format declares rather than hides. **No** field of a build container
enters a context ID — not even the digest (E61, ADR 0018): a context
requires a Zephyr *line*, the backend selects a container serving that
line and records which one in `manifest.yaml`, outside the identity.
SDK ↔ container compatibility is expressed as a constraint over the
coupling labels `org.mcuhome.zephyr` and `org.mcuhome.toolchain`
(carried by the image since r5, alongside `org.mcuhome.contract`), never
as an enumeration of blessed tags, so a CVE respin at the same coupling
is picked up without republishing anything (ADR 0018 decision 7) — and
the line match of E61 is that same mechanism, applied by the backend.

## Consequences

- Reproducible builds everywhere: "works in the container" equals
  "works in CI" equals "works in the add-on". CI walks the same contract
  path a user's build walks, on the same image.
- `--method local-dev` pays the escape hatch's cost visibly: it needs a
  west workspace plus the tools the image would have provided (`gn`,
  `zap`), and missing ones are reported by name before the build starts,
  never as a compiler error ten minutes in.
- Flashing never grows a container-side USB dependency; device
  onboarding rides browser flashing and OTA
  ([draft ADR 0016](draft/0016-device-onboarding-and-flash-transport.md)).
- The exact image layout (base image, layering, registry) is an
  implementation matter documented where the image is built
  (`containers/builder/`); this ADR fixes the principle, and the
  build-container contract fixes the interface any image must satisfy.
