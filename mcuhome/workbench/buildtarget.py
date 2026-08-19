# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Where a build runs, and how it is executed — two axes, not one.

A build has two independent placement questions in it, and the flat list
of method names this package started with answered them in one word,
which is why the list could never stay symmetric: ``local`` says *here, in
a container*, ``remote`` says *over there, however that machine builds*,
and the two are not the same kind of statement at all.

They are separated here:

**Where** — :class:`BuildTarget`. The caller's decision, and the only one
of the two a caller is entitled to make: build on this machine
(:class:`LocalBuild`) or hand the context to a build server
(:class:`RemoteBuild`).

**How it is executed** — :class:`Execution`. A property of the machine
that ends up doing the work. Today there is one: a build container
(:class:`ContainerExecution`). The axis exists anyway, because the second
answer — compiling in a build environment that is already unpacked on the
host, without a container runtime — is a property of *that machine* and
never a client's to state.

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
whole seam :func:`mcuhome.workbench.buildmethods.build_firmware` is built
on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "DEFAULT_MAX_WAIT_SECONDS",
    "BuildTarget",
    "ContainerExecution",
    "Execution",
    "LocalBuild",
    "RemoteBuild",
]

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
    """Compile in a build container, through the invocation ABI.

    The ordinary execution, and the one that needs a container runtime
    and nothing else of a toolchain — which is also why it is the one
    whose private key never reaches the thing that compiles.
    """

    #: Build-container reference to compile in; ``None`` takes the
    #: default the compiler side resolves for the model's Zephyr line.
    image: str | None = None
    #: Where the compiler cache lives on this machine. ``None`` takes the
    #: user's cache directory, which is what every build does unless
    #: somebody moved it — one cache per user, shared by every project.
    ccache_dir: Path | None = None


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
