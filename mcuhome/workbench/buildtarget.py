# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Where a build runs, and how it is executed — two axes, not one.

A build has two independent placement questions in it, and the flat list
of names this package started with answered them in one word, which is
why the list could never stay symmetric: ``local`` said *here, in a
container*, ``remote`` said *over there, however that machine builds*,
and the two are not the same kind of statement at all.

They are separated here, and the configuration says them apart as well:
``build.target`` is the first axis and ``build.mode`` the second.

**Where** — the **target**: :data:`TARGET_LOCAL` or :data:`TARGET_REMOTE`
as the word a configuration writes, :class:`BuildTarget` as the object it
resolves to. The caller's decision, and the only one of the two a caller
is entitled to make: build on this machine (:class:`LocalBuild`) or hand
the context to a build server (:class:`RemoteBuild`).

**How it is executed** — the **mode**: :data:`MODE_CONTAINER` or
:data:`MODE_SUBPROCESS` as the word, :class:`Execution` as the object. A
property of the machine that ends up doing the work. There are two: a
build container
(:class:`ContainerExecution`) and a build environment already unpacked on
the host (:class:`SubprocessExecution`). Which of them a machine uses is
that machine's own property and never a client's to state.

The asymmetry between the two classes is the point rather than an
oversight: :class:`LocalBuild` carries an :class:`Execution` and
:class:`RemoteBuild` carries none. A client does not get to tell somebody
else's machine whether to start a container — that machine's operator
configured it, and a request that overrode them would be a client
reaching past an administrator. What a build server does when a context
reaches it is construct a :class:`LocalBuild` of its own, out of *its*
configuration; and because that is an ordinary construction and not a
special case, a server that is configured to pass the work on constructs
a :class:`RemoteBuild` instead and the multi-hop case needs no code of
its own.

Nothing here reaches a filesystem, a socket or a container. These are the
answers to "where" and "how", stated as data, so that the thing that
answers "what" (the device model, and the build context created from it)
and the thing that answers "where" can travel separately — which is the
whole seam :func:`mcuhome.workbench.build.build_firmware` is built
on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from mcuhome.model.buildenvironment import ENVIRONMENT_IMAGE_REPOSITORY

__all__ = [
    "BUILD_MODES",
    "BUILD_TARGETS",
    "DEFAULT_BUILD_MODE",
    "DEFAULT_BUILD_TARGET",
    "DEFAULT_CONTAINER_PROGRAM",
    "DEFAULT_CONTAINER_REPOSITORIES",
    "DEFAULT_MAX_WAIT_SECONDS",
    "MODE_CONTAINER",
    "MODE_SUBPROCESS",
    "TARGET_LOCAL",
    "TARGET_REMOTE",
    "BuildTarget",
    "ContainerExecution",
    "Execution",
    "LocalBuild",
    "RemoteBuild",
    "SubprocessExecution",
]

#: Where a container build looks for its environment when nobody
#: configured a search list: MCUHome's own repository. Stated here beside
#: the two modes rather than in the profile that searches it, so that the
#: option registry can declare it as the default of
#: ``build.container_repositories`` without importing a build path.
DEFAULT_CONTAINER_REPOSITORIES: tuple[str, ...] = (ENVIRONMENT_IMAGE_REPOSITORY,)

#: The program that runs a build container. ``podman`` is command-line
#: compatible for everything used here, which is the whole reason
#: ``build.container_program`` exists — and the reason this default is
#: stated beside the other build vocabulary rather than inside the
#: profile that drives it: the option registry declares it without
#: importing a build path.
DEFAULT_CONTAINER_PROGRAM = "docker"


#: The two targets, as the words a configuration writes them in: the
#: values of the ``build.target`` key, and the two classes below.
TARGET_LOCAL = "local"
TARGET_REMOTE = "remote"

#: Every target, in the order a refusal lists them.
BUILD_TARGETS = (TARGET_LOCAL, TARGET_REMOTE)

#: What a caller that expressed no preference gets. This machine: a
#: build server is never discovered and has no default, so the only
#: target that can be assumed is the one already here.
DEFAULT_BUILD_TARGET = TARGET_LOCAL


#: The two executions, as the words a configuration writes them in: the
#: values of the ``build.mode`` key. They live here, with the classes
#: they name, so that the option registry can validate the key without
#: importing the module that dispatches builds — a configuration read
#: must not cost the whole build stack.
MODE_CONTAINER = "container"
MODE_SUBPROCESS = "subprocess"

#: Every build mode, in the order a refusal lists them.
BUILD_MODES = (MODE_CONTAINER, MODE_SUBPROCESS)

#: What a caller that expressed no preference gets. The container: it is
#: the mode that needs a container runtime and nothing else of a
#: toolchain, and the only one that isolates a build context — which is
#: untrusted input, because it carries patches.
DEFAULT_BUILD_MODE = MODE_CONTAINER

#: How long a build waits for a turn on a busy build server before it
#: stops. Six hours, and it is not a fairness rule: waiting is bounded so
#: that a build left waiting by something that will never resolve ends on
#: its own. ``0`` removes the bound, which is what a private server
#: wants. It lives here, with the target that uses it, rather than in the
#: module that dispatches — and reading it must not cost the ``remote``
#: extra, which is why it is not in the session client either.
DEFAULT_MAX_WAIT_SECONDS = 21600.0


@dataclass(frozen=True)
class Execution:
    """How a build is executed on the machine that runs it.

    A base with no fields: what the subclasses have in common is the
    question they answer, not any of their answers. Instantiating this
    one is a programming mistake and the dispatch says so by type rather
    than pretending a default.
    """


@dataclass(frozen=True)
class ContainerExecution(Execution):
    """Compile in a build container, per the build environment specification.

    The ordinary execution, and the one that needs a container runtime
    and nothing else of a toolchain — which is also why it is the one
    whose private key never reaches the thing that compiles.
    """

    #: The build environment this one build asks for, in the four pin
    #: forms: a repository, ``:tag``, ``@sha256:…``, or a repository with
    #: either. ``None`` — the ordinary case — searches the configured
    #: repositories for an image whose labels declare the package set the
    #: context pinned.
    image: str | None = None
    #: Where the compiler cache lives on this machine. ``None`` takes the
    #: user's cache directory, which is what every build does unless
    #: somebody moved it — one cache per user, shared by every project.
    ccache_dir: Path | None = None


@dataclass(frozen=True)
class SubprocessExecution(Execution):
    """Compile in a build environment unpacked on this host, without a container.

    The second answer to "how", and the one the module docstring
    anticipated: the environment's packages are unpacked into a per-user
    store and the builder runs as an ordinary child process. It needs no
    container runtime and isolates nothing — the build runs with the
    calling user's rights — so it is the execution for a machine whose
    builds are its own, and never the one a build server offers to
    strangers.

    Like :class:`ContainerExecution` this is a statement about *this*
    machine. A :class:`RemoteBuild` carries no execution at all, so a
    client can no more ask a build server to run without a container than
    it can ask it to run with one.
    """

    #: Where the compiler cache lives on this machine, as for a container
    #: build: the parent of the two role directories. ``None`` builds
    #: without a durable cache, which is slow rather than wrong.
    ccache_dir: Path | None = None
    #: A development build: the west workspace to build against
    #: **instead of** the environment MCUHome would provision into its
    #: store — the developer's own, so that a change in it can be built
    #: without publishing a package first. Its manifest repository is the
    #: SDK that gets compiled and the tools are the ones on the ``PATH``
    #: the build was started from, so this one path names the whole
    #: environment. A build context that carries patches is refused in
    #: this form rather than applied to a workspace somebody else owns.
    dev_workspace: Path | None = None
    #: An image somebody stated for this build although it starts none —
    #: a configured builder's ``image:``, which is a statement about the
    #: machine rather than about this build. Carried so the build can say
    #: once that it has no effect here; nothing reads it as a pin.
    stated_image: str | None = None


@dataclass(frozen=True)
class BuildTarget:
    """Where a build runs. A base with no fields, like :class:`Execution`."""


@dataclass(frozen=True)
class LocalBuild(BuildTarget):
    """Build on this machine, in the stated execution."""

    #: How this machine executes the build. Defaults to a build
    #: container: it is what a caller that expressed no preference gets,
    #: and the only execution that does not depend on the caller having a
    #: toolchain.
    execution: Execution = field(default_factory=ContainerExecution)


@dataclass(frozen=True)
class RemoteBuild(BuildTarget):
    """Hand the build context to a build server.

    Carries **no** :class:`Execution`: see the module docstring. What it
    carries instead is everything about reaching that server and about
    what to do when it is busy.
    """

    #: The build server's address, as a person writes it: a host, a
    #: ``host:port``, or either with a scheme.
    server: str | None = None
    #: The bearer token for it. ``None`` sends no ``Authorization``
    #: header at all, which this package permits because a third-party
    #: server may want none.
    token: str | None = None
    #: Wait when the build server has no room. A busy server hands out a
    #: turn instead of a session, and waiting for it is what a person
    #: starting a build almost always wants; ``False`` is the caller that
    #: would rather be told now.
    wait: bool = True
    #: How long that wait may last in total, in seconds. ``0`` removes
    #: the bound.
    max_wait_seconds: float = DEFAULT_MAX_WAIT_SECONDS
    #: The build environment this one build asks for, in the four pin
    #: forms a container build takes. It travels with the context and
    #: narrows what the *server* looks at; which repositories may be used
    #: at all stays that server's operator's decision. Empty is the
    #: ordinary case: the server searches what it allows.
    image: str | None = None
