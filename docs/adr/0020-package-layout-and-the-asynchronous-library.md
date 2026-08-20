# 0020 — Package layout and the asynchronous library

- Status: accepted
- Date: 2026-08-09

## Context

ADR 0017 fixed the repository layout for the remote-build architecture
and named one published Python distribution, "the lib": what remained
of this repository's Python package once the command shell moved to its
own repository. The 2026-08-08/09 remote-build work makes that one
package wrong in three separate ways.

**It has to run in a place that cannot have all of it.** Code
generation now runs inside the build container, out of the SDK package
mounted there — a process with no configuration tree, no session
client, no user keys and no reason to carry any of them. Meanwhile the
same code base must run in a dashboard backend that must never carry a
toolchain (ADR 0017 §2).

**One consumer needs the vocabulary without the logic.** ADR 0019 §8
obliges the build server to recompute every file hash and the context
ID from the received bytes, and ADR 0018 §6 states that rule as frozen
and computed identically by both sides of the contract. ADR 0017 §3
nevertheless says the build server "does not consume the lib at all",
which leaves that one frozen rule to be implemented twice, in two
repositories, with nothing that pins the two implementations to the
same value.

**Its contract is synchronous, and the thing it will do is wait.** The
supported surface documents itself as synchronous and CPU-bound and
tells callers with an event loop to use an executor (`api.py:47-48`);
the dashboard repeats that instruction for its own wrapper
(`dashboard/backend/mcuhome/ui/builder.py:105-106`) and offloads
every call with `asyncio.to_thread`
(`dashboard/backend/mcuhome/ui/commands.py:246`, `:408`). That
was adequate while the surface was YAML parsing. It is not adequate for
a surface whose principal operations are a compile and a session
protocol.

And the name itself is a defect: "lib" named the extraction history —
the remainder after the CLI split — not the thing.

## Decision

### 1. Three packages, split by where the code has to run

| Package | What it holds | Where it runs |
|---|---|---|
| `mcuhome-model` | Device model, registry (boards, drivers, clusters, partitions, update schemes), the context format including the frozen ID rule of ADR 0018 §6, error types, version constants. **No I/O.** | Everywhere |
| `mcuhome-workbench` | Stages 1-3, pin resolution, context creation, the three build methods (decision 6), the session-protocol client, signing. | Wherever a build is driven: the CLI, the dashboard, third-party embedders |
| `mcuhome-compiler` | Stages 4-5 — code generation, west orchestration, artifact collection, build report — plus the invocation-ABI adapter (decision 7). | Inside the build container; shipped in the SDK package |

The line is drawn by execution site, not by subject matter. That is why
`mcuhome-compiler` is not "the back half of the build pipeline" but a
deliverable of the SDK package: a build container executes MCUHome's
code generation out of the mounted SDK, which is what makes "bring your
own build container" mean own toolchain and own Zephyr rather than own
build logic.

`mcuhome-model` is the part every one of those sites needs and none of
them may re-derive — most sharply the context ID, whose entire purpose
is that independent parties compute the same value.

### 2. The CLI package becomes `mcuhome`; "lib" is retired

This repository's distributions renounce the plain name: no package
here may claim `mcuhome`, because `pip install mcuhome` should yield
the command a user expects, and no other package has a better claim
to the plain name. The command line's half — its distribution bearing
that name, the console script unchanged — is recorded in cli
ADR 0002. The services keep `mcuhome-ui` and
`mcuhome-buildserver`.

"lib" is retired as a term. It named where the package came from rather
than what it is, and it could only ever mean "the rest" — a name three
packages with three jobs cannot share.

### 3. `mcuhome-compiler` is an optional extra of `mcuhome-workbench`

`mcuhome-workbench` depends on `mcuhome-compiler` for the `local-dev`
method only, as an optional extra, never as a base dependency. Only
`local-dev` executes stages 4-5 in the caller's own process; `local`
and `remote` reach the very same stages through a build container that
already carries them (decision 6). An installation that will never
build in-process therefore never installs the compiler.

### 4. The build server consumes `mcuhome-model`, and nothing else

This replaces ADR 0017 §3's "the build server does not consume the lib
at all" and keeps its intent exactly: the build server depends on no
build logic, only on the shared vocabulary. It orchestrates; it is
never a build environment, and it stays able to orchestrate third-party
build containers because nothing it links against knows how to build.

The absolute form failed on one obligation. ADR 0019 §8 makes the
server recompute the context ID from received bytes, and ADR 0018 §6
freezes the rule for that computation and says both sides of the
contract compute the same ID. Under "consumes nothing", that rule gets
a second implementation in a second repository, with no conformance
vectors between them — two chances to disagree about a value whose only
job is to be identical on both sides, and a disagreement that surfaces
as a rejected upload or a misattributed artifact rather than as a test
failure.

The build-server code already performs this reduction on its own
account: it spawns the build program as a subprocess and imports the
library only for the version constants
(`build-server/mcuhome/buildserver/builder.py:30-32`, until that file
went with the job protocol) — which are
`mcuhome-model` contents.

### 5. Everything a caller waits on is awaitable

**The commitment, stated for third-party embedders as much as for our
own services:** every operation a caller waits on is offered as an
awaitable, and nothing that blocks meaningfully is offered only
synchronously. Pure computation stays synchronous. The line falls along
the package split:

- `mcuhome-model` never sees an event loop.
- `mcuhome-compiler` keeps synchronous cores and adds one async driver
  for the compile subprocess; the invocation-ABI adapter uses the
  synchronous path.
- `mcuhome-workbench` is async throughout: the three build methods, the
  session client, event streaming, cancellation.

The measured reasoning, which is why the line falls there and not
elsewhere:

- **Making the computation awaitable buys nothing.** Stage 4 takes
  19 ms and stages 1-3 take 21 ms, against a build that blocks for
  1:12 to 13:38. The asymmetry is three orders of magnitude; there is
  no event loop to be starved by 40 ms and no build to be survived
  without an await.
- **An event loop the compiler does not need is a cost it pays per
  command.** `import mcuhome.api` costs 150 ms, and the in-container
  process pays that on every invocation, for a strictly serial job.
- **The synchronous core is what keeps synchronous embedding
  possible.** A synchronous facade over an async core cannot be built
  safely, because `asyncio.run` raises inside any caller that already
  has a running loop. The converse direction is always available: an
  async driver over a synchronous core composes for both kinds of
  caller. So the core stays synchronous and the waiting is async, never
  the reverse.

What the commitment buys back is the pair of capabilities the current
layout cannot express. The dashboard embeds the library in-process and
offloads it with `asyncio.to_thread`, which can neither stream a
subprocess's output nor cancel it — which is precisely why the build
server imports nothing and spawns the CLI instead, and says so with its
three reasons (`build-server/mcuhome/buildserver/builder.py:3-28`,
read at `8b8ceb4`; the file has since been removed with the job
protocol, and the build server imports nothing from this package today).
Streaming is what ADR 0019 §3's typed progress stream and separate raw
log stream *are*; cancellation is the precondition for any verb that
aborts a running invocation. Both are unreachable across a thread
boundary and ordinary across an await.

### 6. Three build methods behind one interface

- **`local-dev`** — a local west workspace and locally installed tools;
  runs the build directly. This is MCUHome's own development loop.
- **`local`** — drives the build container on the local machine via the
  container runtime.
- **`remote`** — the same, via a build server.

`mcuhome-workbench` abstracts all three fully; the choice is
transparent to the user, who selects a method, not a code path. ADR
0019's observation that a local and a remote build are the same
protocol extends here to the layer above it: they are also the same
call.

The recursion is the point. In `local` and `remote`, the code running
inside the build container is **the same code base performing a
`local-dev` build there**. There is one build implementation; the three
methods are three ways of reaching it.

### 7. The invocation-ABI adapter is not a second build implementation

The adapter in `mcuhome-compiler` translates the container invocation —
the command plus its request document — into a `local-dev` build of
that same code base. It is an adapter and only an adapter. It
implements no build step of its own, so it cannot drift from the build
the developer runs on their own machine, and every capability
`mcuhome-compiler` gains is reachable through it without touching the
frozen invocation shape. That shape itself belongs to the
build-container contract, not to this ADR.

### 8. One version, one tag, one repository

`mcuhome-model`, `mcuhome-workbench` and `mcuhome-compiler` all carry
the **single shared version of ADR 0017 §3**, and all three live in and
are published from the `mcuhome` repository. One release and one tag
covers the three packages together with the `mcuhome-sdk-<version>`
archive and the build-container image.

ADR 0017 §3's argument carries across the split unchanged, because it is
an argument about the repository and not about how many distributions
that repository emits. Validation is defined against the registry; the
registry describes what the C runtime can actually do; and the golden
tests (ADR 0014) pin generator output against the C sources in the same
CI, on the same commit, as both sides they compare. Splitting the
packages moves code between distributions, not between those three
facts. "Which package works with which SDK" therefore stays a question
that cannot be asked — the property §3 bought, and the one an
independent version would sell.

`mcuhome-workbench` is the package that would gain most from versioning
independently: it is the one of the three that neither ships inside the
SDK archive nor is consumed by the build server. It is also the one
whose independent version would reintroduce the matrix most directly,
because it resolves pins, creates contexts and computes context IDs
against the same registry and the same frozen rule (ADR 0018 §6) as
everything else in the release.

The price is stated plainly rather than argued away: **a fix in the
session-protocol client formally requires an SDK release.** That is
accepted. A release carrying an otherwise unchanged SDK costs a version
number once; a compatibility matrix costs every reader of it, forever.

## Consequences

- **ADR 0017 is superseded in part.** Its §1 table entry naming a
  single **lib** package, and the use of that name in §2 and §3, are
  replaced by decision 1; §3's "the build server does not consume the
  lib at all" is replaced by decision 4. What is *not* touched: the
  four-repository layout of §1, the repo ≠ package rule of §2, and §3's
  three reasons for one shared version (atomic contract changes,
  colocated golden tests, no compatibility matrix) — those are
  arguments about the repository, and this is a packaging split.
  ADR 0017's release-process consequence changes count: "three
  artifacts, one version, one tag" becomes the SDK package, the
  build-container image and the packages of decision 1.
- **ADR 0017 §3's single shared version covers all three packages, and
  this repository publishes all three** (decision 8). ADR 0017's
  amendment recorded both as open and now carries the answer. The
  packaging split therefore adds no release step: the same tag that cuts
  the SDK archive and the build-container image cuts `mcuhome-model`,
  `mcuhome-workbench` and `mcuhome-compiler`.
- **No definition of done is fixed up front.** The quality criteria for
  v1.0 are set once the end-to-end path exists and its cost is visible.
  This ADR splits one package into three, turns a documented synchronous
  contract into an awaitable one and leaves the migration to a later
  phase; acceptance criteria written now would be written against an
  estimate of work whose shape this ADR set has only just settled. They
  are set from the measured path, not ahead of it.
- **The documented synchronous contract changes.** `api.py:47-48`
  states synchrony as a property of the whole supported surface;
  `dashboard/backend/mcuhome/ui/builder.py:105-106` repeats it
  for the dashboard's wrapper. Under decision 5 that is no longer
  expressible module-wide: it holds for the computation and stops
  holding at the build methods and the session client. Both docstrings
  are rewritten to state synchrony per operation, and the dashboard's
  `asyncio.to_thread` offload sites
  (`dashboard/backend/mcuhome/ui/commands.py:246`, `:408`)
  become direct awaits as the operations they wrap become awaitable.
- **Process-global state has to go, and it is not incidental.** Two
  shapes of it. The first is the installed-location assumption:
  `MODULE_DIR` was `Path(__file__).resolve().parent.parent`, which says
  *this library lives inside a west workspace* — true for `local-dev`
  and false for every other execution site in decision 1. The second is
  the call-time read: a function that consults `Path.cwd()`,
  `os.environ` or `Path.home()` when it runs answers from the process
  rather than from what it was given, so one process cannot serve two
  concurrent sessions with different working directories and
  environments. Decision 5 makes concurrent sessions in one process the
  normal case, so both were fixed on the way rather than after. The
  module directory is now a parameter of `plan_build`, with
  `workspace.installed_module_dir()` as the answer the *command line*
  supplies because the command line is the local-dev case; the working
  directory and the environment are arguments everywhere; and
  `mcuhome/model/userpaths.py` resolves the per-user directories from the
  environment it is handed — refusing rather than guessing when that
  environment names no home, because the directory in question holds a
  private signing key. `tests_py/test_userpaths.py::test_no_module_reads_process_state`
  reads every module of the package as a syntax tree and fails the suite
  if any of them reaches for the process again.
- The plain distribution name `mcuhome` (decision 2) collides with the
  import package this repository ships under that name today. Resolving
  it — import names for the three packages, and what a caller writes
  after `import` — is migration work for the merge plan, which ADR 0017
  already defers to the phase after the ADR set. **The shape of that
  migration is decided** (product owner, 2026-08-09): packaging and
  import names move in **one** migration, to real subpackages
  (`mcuhome.model`, `mcuhome.workbench`, `mcuhome.compiler`), with no
  interim phase in which three distributions deliver into the flat
  package — three distributions claiming file subsets of one directory
  is a relationship pip cannot express, and uninstalling one would
  silently gut the other two. Until that migration lands, nothing may
  re-implement what `mcuhome-model` will own; a verb that needs the
  context-ID computation refuses typed rather than shipping sooner on
  a second implementation of the frozen rule.
- The build server's dependency becomes a package rather than a
  contract document alone. ADR 0017 §1's "depends on: builder contract"
  gains `mcuhome-model`; the contract remains what makes third-party
  build containers orchestratable.
- Related standing decisions: ADR 0005 (SemVer), ADR 0007
  (containerized toolchain), ADR 0014 (golden tables contract),
  ADR 0015 §8 (client-side signing), ADR 0017 (superseded in part, see
  above), ADR 0018 (context format and the frozen ID rule), ADR 0019
  (session protocol and the build-container contract); dashboard
  ADR 0011 (in-process embedding), dashboard ADR 0012.
