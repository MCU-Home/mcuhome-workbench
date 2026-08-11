# 0007 — Containerized toolchain, minimal host requirements

- Status: accepted; amended 2026-08-09 (see the amendment at the end)
- Date: 2026-08-02

## Context

Zephyr development normally requires a locally installed Zephyr SDK
(cross-compilers for every target architecture), west, Python tooling and
per-vendor flash utilities — a heavy, error-prone host setup. MCUHome must
additionally run as a Home Assistant add-on, where the entire build system
has to live inside a single Docker container anyway. The Zephyr project
publishes official CI/build container images
(`ghcr.io/zephyrproject-rtos/ci`) that bundle the SDK.

## Decision

Product-owner requirement: keep host prerequisites minimal.

- **On the developer machine:** only ubiquitous tools — git, docker, and
  optionally make/cmake for convenience wrappers. No Zephyr SDK, no
  cross-compilers, no vendor flash tools on the host.
- **In containers:** toolchains, Zephyr SDK, west, codegen, and flashing
  utilities. The MCUHome builder image is the single build environment,
  used identically by developers, CI, and the Home Assistant add-on.
- Container images are versioned in lockstep with the Zephyr pin in
  `west.yml` (one image per MCUHome release).

## Consequences

- Reproducible builds everywhere; "works in the container" equals "works
  in CI" equals "works in the add-on".
- Flashing from inside containers needs USB passthrough, which is fiddly
  (especially under Home Assistant OS) — device flashing will therefore
  prefer browser-based flashing (WebSerial/WebUSB, as popularized by ESP
  Web Tools) and OTA updates over container-side USB access. Details are
  a design-phase topic.
- The exact image layout (base image, layering, registry) is decided when
  the builder is implemented; this ADR fixes the principle.

## Amendment: one conforming build container among several (2026-08-09)

The principle above is unchanged: the host needs git and docker, the
toolchain lives in a container. Two of its formulations are not.

**"The MCUHome builder image is the single build environment" no longer
holds.** The build container is now a replaceable part: any container
satisfying the published contract is a usable build container,
third-party ones with their own toolchains included, and it is driven
through a frozen invocation ABI rather than by knowing what is inside it
(ADR 0019 decision 4;
[build-container-contract.md](../design/build-container-contract.md)).
MCUHome's own image is the reference implementation of that contract and
one conforming build environment among possibly several. The contract
also bounds what "your own" may mean: a conforming build container must
execute MCUHome's code generation out of the SDK tree it is handed, so
"bring your own build container" is own toolchain and own Zephyr, never
own build logic (contract §6.1). The terms are kept apart deliberately:
a build container is the build environment and never a service, and a
build server "orchestrates; it is never a build environment" (ADR 0020
decision 4) — which is what keeps it able to drive build containers it
did not build.

**Image identity is the digest, not the tag.** The lockstep versioning
above stays a release-process rule and stops being an identity rule: a
backend names a chosen image by its digest and never by its tag
*wherever the image has a repo digest*, while `container.image` and
`container.tag` are the human-readable trail beside it. An image built
on the host and never pushed has no repo digest; it is served, named by
the tag its host lists it under, and recorded `digest: null` (contract
§3.2) — the honest answer for bytes nobody can fetch, and a window the
format declares rather than hides. **No** field of a build container enters a context ID — not
even the digest, since E61 (ADR 0018's amendment of 2026-08-11): a
context requires a Zephyr *line* and the backend records which container
answered it, outside the identity. SDK ↔ container compatibility is
expressed as a constraint over the coupling labels `org.mcuhome.zephyr`
and `org.mcuhome.toolchain`, never as an enumeration of blessed tags, so
a CVE respin at the same coupling is picked up without republishing
anything (ADR 0018 decision 7) — and the line match E61 introduced is
that same mechanism, applied by the backend.

Terminology: the image this ADR calls the "builder image" is the **build
container** / build-container image throughout the newer documents.
