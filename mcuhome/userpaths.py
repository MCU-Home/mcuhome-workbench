# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Per-user directories, resolved from a stated environment.

Three things in this package live under the invoking user's home
directory: the firmware signing key (``$XDG_CONFIG_HOME/mcuhome/``,
ADR 0015 decision 8), the build cache the container mounts
(``$XDG_CACHE_HOME/mcuhome/``) and any path a user wrote with a leading
``~``. All three used to be resolved with :meth:`pathlib.Path.home` and
:meth:`pathlib.Path.expanduser`, which read ``HOME`` out of the *process*
environment at the moment they are called.

That is the pattern ADR 0020 rules out. The library is becoming one
process that serves several sessions, each with its own environment, and
a function that takes an ``env`` argument and then quietly consults the
process anyway is worse than one that never took the argument: it
documents a promise it does not keep. The concrete failure is not
hypothetical — a build server started by systemd runs with no ``HOME``
at all, and :meth:`~pathlib.Path.home` answers that with the account
database, so the *server's* signing key would be resolved for a session
that named a different one.

So home is an input here, and an absent one is an error rather than a
guess. :func:`home` refuses instead of falling back, because every way of
falling back picks a directory the caller did not ask for, and the
directory in question holds a private key.

``~user`` (a leading tilde followed by an account name) is the one form
that is *not* environment-derived: it names an account, and the account
database is the only place that answers it. :func:`expand` hands that
form to :meth:`~pathlib.Path.expanduser` unchanged.

This module is pure: it computes paths and touches none of them.
"""

from __future__ import annotations

from pathlib import Path

from mcuhome.errors import ConfigError

__all__ = [
    "HOME_VAR",
    "expand",
    "home",
]

#: The environment variable that says where the user's home directory is.
HOME_VAR = "HOME"


def home(env: dict[str, str]) -> Path:
    """The home directory *env* describes.

    Raises :class:`~mcuhome.errors.ConfigError` when *env* names none —
    see the module docstring for why this does not fall back to the
    process. Callers that have a way to avoid the home directory
    entirely (an explicit path, ``XDG_CONFIG_HOME``, ``XDG_CACHE_HOME``)
    should say so in the hint they let through.
    """
    from_env = env.get(HOME_VAR)
    if not from_env:
        raise ConfigError(
            f"no {HOME_VAR} in the environment, so there is no home directory to "
            f"resolve a per-user path against",
            hint=(
                f"set {HOME_VAR}, or name the path outright — services started "
                f"without a login session often have no {HOME_VAR}"
            ),
        )
    return Path(from_env)


def expand(path: Path | str, env: dict[str, str]) -> Path:
    """*path* with a leading ``~`` resolved against *env*.

    The drop-in replacement for :meth:`pathlib.Path.expanduser` in code
    that has an environment to answer from. Paths without a leading tilde
    come back untouched, which is the overwhelming majority of them.
    """
    result = Path(path)
    first = result.parts[0] if result.parts else ""
    if not first.startswith("~"):
        return result
    if first != "~":
        # ``~someone`` — an account name, not this environment's user.
        return result.expanduser()
    return home(env).joinpath(*result.parts[1:])
