# 0026 — Where a build sits in its container, and where its cache lives

- Status: draft
- Date: 2026-08-16

Product-owner design round of 2026-08-16. Project-wide: it decides the
mount layout of both `container`-profile backends (the workbench's
`local` build method and the build server's session backend), and it
changes what the build-container image does about ccache. It refines
ADR 0019 §6 and build-container contract §10 without changing what
either one requires of a *program*.

## Context

**The cache was never warm.** A real build of the reference device made
1318 cacheable compiles and took **2** of them from the cache — 0.15 %.
It wrote 94 MB into `<work>/ccache` and the next build deleted them: the
program put its cache in the session's working area (contract §10's
"absent" branch), and every build wipes that area before it starts. The
local backend never filled the request document's `ccache` field, so
that branch was the only one anybody took.

**Making the cache survive would not have been enough.** Zephyr appends
three `-fmacro-prefix-map=<absolute path>=…` options to every single
compile — for the application source, `ZEPHYR_BASE` and the west topdir
— and compiles with `-g -gdwarf-4`, which makes ccache hash the working
directory as well (`hash_dir`, on by default, because the object records
that directory). Both are correct behaviour. Their consequence is that
the backend's directory layout is part of every cache key: with mounts
at host paths, each project directory has a cache of its own, and a
store warmed anywhere else is dead weight.

Measured, same sources, two project directories:

| | hits |
|---|---|
| host paths, no `base_dir` | 0 of 2 |
| host paths, `base_dir` per project | 0 of 2 — `-g` puts the working directory in the key |
| host paths, `base_dir` **and** `hash_dir = false` | 1 of 2, and the object carries the *first* project's directory in its debug information |
| same paths in every container | 2 of 2 |

**Where the mounts came from.** Both container backends mounted every
directory at its own host path (`-v /home/…/work:/home/…/work`). That
was deliberate: the request document then names paths that mean the same
thing on both sides, which makes a stalled build inspectable and a
compiler error copy-pasteable. Contract §4 fixes no mount points and
forbids a *program* from depending on one, so both choices were always
open to a backend.

## Decision

### 1. Every session sits at the same paths inside its container

`mcuhome.model.containerpaths`, used by both `container`-profile
backends:

| | |
|---|---|
| `/mcuhome/ctx` | the build context, read-only |
| `/mcuhome/work` | the session's working area |
| `/mcuhome/inv/<invocation id>` | one invocation's `out`, `tmp`, request, result, events, cancel |
| `/mcuhome/workspace/mcuhome-sdk` | the SDK, at the path `describe` declares — unchanged |
| `/mcuhome/sdk` | the SDK when `describe` declares no path |
| `/ccache/cache-local`, `/ccache/cache-shared` | the compiler cache, see 3 |

This is the backend convention §4 permits ("`/ctx`, `/out` and `/ccache`
may be used as conventions by an image or a backend"), not an exception
to it. The paths are still *stated* in the request document, the program
still reads them there, and the conformance suite still moves every one
of them.

What it buys, beyond the cache: a build cannot tell a local backend from
a build server's, the container never learns the host's directory
layout (and a remote build's log stops quoting the *server's*), and one
sentence of documentation answers "where is my build tree" for everyone.

What it costs is that a path in a compiler message is not a path on the
host. **Deliberately not compensated** (PO): the container path carries
information a host path does not — that this came out of a build
container rather than a `local-dev` build — it is shorter, and it leaks
no user name when somebody pastes it. Source-level debugging is a
`local-dev` activity; a debugger on a container build needs
`substitute-path` either way, because Zephyr and CHIP live in the image
and never had host paths.

**`-ffile-prefix-map` is not the answer to that** and must not be added:
the flag is part of the command line, so a host-specific value would put
the host back into every cache key.

### 2. The `subprocess` profile keeps host paths

Several sessions share one filesystem namespace there and cannot all
have `/mcuhome/work`. Giving them one would mean mount namespaces
(`unshare`, bubblewrap) and therefore privileges a Home Assistant add-on
cannot count on — a kernel dependency for the profile whose purpose is
to need no container.

When that profile is next worked on, it serves **one session at a time**
(PO): a second is refused or waits. Its typical deployment is one person
on one machine who can build one image at a time anyway. Until then the
profile is unchanged, and `SessionBackend._inside` is the seam — the
container backend maps host paths to container paths, the subprocess
backend answers with the path itself.

### 3. The image configures ccache; the backend mounts

The image (r9) states both of ccache's roles in `/etc/ccache.conf`:

```
cache_dir      = /ccache/cache-local
remote_storage = file:/ccache/cache-shared|read-only
```

and `CCACHE_DIR` leaves the environment, because an environment variable
overrides that file rather than agreeing with it. A build is therefore
never told about a cache, and a backend decides everything by mounting:

| on `cache-local` | on `cache-shared` | effect |
|---|---|---|
| nothing | nothing | the cache dies with the container — §10's "absent" case exactly |
| a host directory | nothing | it survives; the next build starts warm |
| a host directory | a store somebody else filled | as above, and the first build starts warm too |

ccache copies what it finds in the read-only store into the writable
one, so warming needs no command. `base_dir` stays unset and `hash_dir`
stays on: with identical paths there is nothing to normalize, and the
table above says what turning `hash_dir` off would cost.

Contract §10's request field is untouched and stays mandatory for a
program to honour — it is what a backend serving a foreign image, or one
whose store is not a mount, still has. §10.1 records that an image may
answer for itself.

### 4. One cache per user, not per project

`$XDG_CACHE_HOME/mcuhome/ccache/{cache-local,cache-shared}` (Windows:
`%LOCALAPPDATA%`, which does not roam — `%APPDATA%` would copy five
gigabytes to a file server at every logon). Configurable as `ccache_dir`
through all five layers.

Per-project isolation was considered and rejected. A ccache entry is
addressed by a content hash — the preprocessed source, the compiler's
own bytes, the normalized command line — so two projects share an entry
exactly when the compilation is the same compilation, and anything that
differs about a device changes its generated configuration and therefore
the key. A per-project split would protect nothing and would cost the
sharing this ADR exists to create; the bulk of the objects (Zephyr,
CHIP) is identical across every project. The remaining risk is a damaged
cache file, against which partitioning does not help either — deleting
does, and costs only time.

**Isolation between parties is a different question.** A build server
serving people who do not trust each other needs it, and gets it the way
§10 already says: the shared store read-only. A per-client writable
cache there is a later step, keyed on client identity rather than on
project — it belongs with authentication and is not decided here.

A host directory rather than a named docker volume: a fresh named volume
is root-owned and a container running as the calling user cannot write
to it; the shared store is meant to be filled from outside, and there is
no way into a named volume without starting a container; the cache has
to be listable and deletable when docker is not running at all. On Linux
both are the same bind mount underneath, so no performance is traded
away — where it differs (a VM-backed docker, a home directory on a
network filesystem), `ccache_dir` moves it.

An environment that names no home directory gets **no cache and still
builds**. The refusal `mcuhome.model.userpaths.home` raises was written
for the signing key, where guessing a directory would be wrong; a cache
is an optimization.

## Consequences

- Image revision r9. The `org.mcuhome.*` labels and the program are
  unchanged, so nothing about conformance moves.
- Both container backends change their mount targets; the request
  documents they write carry no host path any more. Pinned by tests in
  both repositories.
- `mcuhome doctor` reports the cache location and how full it is —
  otherwise nothing would ever mention the one directory MCUHome keeps
  outside the project.
- A build directory produced by the `local` method is no longer usable
  by a `local-dev` build of the same device: the CMake tree records
  container paths. The two methods were never meant to share a tree —
  `local-dev` builds out of a developer's own west workspace — and
  nothing in the pipeline reads one method's tree with the other.
