# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The configuration model: five layers, one option registry (ADR 0022).

Every option is declared exactly once, in :data:`OPTIONS` — name, type,
default, and **which channels may set it** — and the ``MCUHOME_*``
variable, the command-line flag spelling and the configuration key all
derive from that declaration. Not every option belongs in every
channel: a per-invocation value is argument+environment only and never
lives in a static file, and the two bootstrap options stand outside the
merge entirely (:mod:`mcuhome.workbench.project` resolves them first,
because they decide where the project layer even is).

The five layers, ascending — later wins::

    system       /etc/mcuhome/configuration.yaml
    user         $XDG_CONFIG_HOME/mcuhome/configuration.yaml
    project      mcuhome.yaml in the project directory
    environment  MCUHOME_* variables
    command      the invocation's arguments

The system/user files are deliberately **not** named ``mcuhome.yaml``:
only a project directory may look like a project directory, and a
config directory must never be mistaken for one — by the upward search
or by a user working inside it (ADR 0022 §2). The directories follow
the platformdirs conventions the ADR names, computed here from the
*stated* environment rather than through the platformdirs library,
because that library answers out of the process environment and this
package serves several sessions from one process (ADR 0020,
:mod:`mcuhome.model.userpaths`). On Windows the conventional homes are
``%ProgramData%\\mcuhome`` and ``%APPDATA%\\mcuhome``, from the stated
environment too; a layer whose directory the environment cannot name
simply does not exist for that resolution.

Merge semantics: scalars are nearest-wins, whole value per layer.
Structured values define their own rule where they are introduced —
builder lists merge by name (ADR 0023 §3). ``mcuhome config print``
falls out of the same registry: :meth:`Settings.print_data` answers
with every effective value and the layer it came from.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcuhome.model.errors import ConfigError, Location
from mcuhome.model.userpaths import config_dir, expand

from mcuhome.workbench.loader import load_yaml_file
from mcuhome.workbench.project import Project

__all__ = [
    "CONFIG_FILE",
    "OPTIONS",
    "Option",
    "Setting",
    "Settings",
    "option",
    "resolve_settings",
    "system_config_dir",
    "user_config_dir",
]

#: File name of the system and user layers. Deliberately not
#: ``mcuhome.yaml`` — the module docstring says why.
CONFIG_FILE = "configuration.yaml"

#: Origin labels, in ascending precedence. ``default`` is what a value
#: has when no layer set it.
_ORIGINS = ("default", "system", "user", "project", "environment", "arguments")


@dataclass(frozen=True)
class Option:
    """One declared option — the single source of every spelling.

    *kind* is one of ``string``, ``path``, ``paths`` (an ordered list,
    ``os.pathsep``-separated in the environment) and ``integer``.
    The three channel switches say where the option may be set:
    *files* covers all three file layers at once — there is no option
    that a user file may set and a system file may not. *bootstrap*
    marks the two options that run before the merge; they are declared
    here so their spellings derive like everyone else's, but
    :func:`resolve_settings` refuses them from files and skips them in
    the merge (:func:`mcuhome.workbench.project.resolve_project` is
    their resolver).
    """

    name: str
    kind: str
    default: Any = None
    files: bool = True
    environment: bool = True
    arguments: bool = True
    bootstrap: bool = False
    help: str = ""

    @property
    def env_var(self) -> str:
        return "MCUHOME_" + self.name.upper()

    @property
    def flag(self) -> str:
        return "--" + self.name.replace("_", "-")


def option(name: str, registry: tuple[Option, ...] | None = None) -> Option:
    """The declaration of *name*, or a ``ValueError`` for a name nobody declared."""
    for declared in OPTIONS if registry is None else registry:
        if declared.name == name:
            return declared
    raise ValueError(f"{name!r} is not a declared option")


#: The platform's option registry. Tools may resolve additional
#: registries of their own through the same machinery (the CLI's
#: presentation options, say) — these are the options the *platform*
#: owns, shared by every tool (ADR 0022 §4).
OPTIONS: tuple[Option, ...] = (
    Option(
        "project_dir",
        kind="path",
        files=False,
        bootstrap=True,
        help="the project directory; disables the upward marker search (ADR 0022 §2)",
    ),
    Option(
        "sdk_sources",
        kind="paths",
        default=(),
        help="directories holding hash-pinned MCUHome SDK packages (ADR 0018)",
    ),
    Option(
        "signing_key",
        kind="path",
        files=False,
        help="a firmware signing key file to use instead of the project's (ADR 0015 §8)",
    ),
    Option(
        "jobs",
        kind="integer",
        default=1,
        help="parallel compile jobs a build may use",
    ),
)


@dataclass(frozen=True)
class Setting:
    """One resolved value, with the layer it came from."""

    option: Option
    value: Any
    #: One of ``default``, ``system``, ``user``, ``project``,
    #: ``environment``, ``arguments``.
    origin: str
    #: Where exactly: the file for a file layer, the variable name for
    #: the environment, the flag for an argument, ``None`` for a default.
    source: str | None = None


class Settings:
    """The resolved configuration: every declared option, one value each."""

    def __init__(self, settings: dict[str, Setting]):
        self._settings = settings

    def __contains__(self, name: str) -> bool:
        return name in self._settings

    def setting(self, name: str) -> Setting:
        if name not in self._settings:
            raise ValueError(f"{name!r} is not a declared option")
        return self._settings[name]

    def value(self, name: str) -> Any:
        return self.setting(name).value

    def origin(self, name: str) -> str:
        return self.setting(name).origin

    def print_data(self) -> dict[str, dict[str, Any]]:
        """What ``mcuhome config print`` renders: value and origin per option.

        JSON-ready — paths become strings — and in declaration order,
        which groups related options the way the registry does rather
        than alphabetically tearing them apart.
        """

        def jsonable(value: Any) -> Any:
            if isinstance(value, Path):
                return str(value)
            if isinstance(value, tuple):
                return [jsonable(item) for item in value]
            return value

        return {
            name: {
                "value": jsonable(setting.value),
                "origin": setting.origin,
                "source": setting.source,
            }
            for name, setting in self._settings.items()
        }


def system_config_dir(env: Mapping[str, str]) -> Path | None:
    """The system configuration directory, or None when the platform names none.

    ``/etc/mcuhome`` on POSIX; ``%ProgramData%\\mcuhome`` on Windows,
    from the stated environment. ``None`` — rather than an error —
    because an absent system layer is a normal machine, not a broken
    one.
    """
    if os.name == "nt":
        base = env.get("ProgramData")
        return Path(base) / "mcuhome" if base else None
    return Path("/etc/mcuhome")


def user_config_dir(env: Mapping[str, str]) -> Path | None:
    """The user configuration directory, or None when *env* names none.

    The XDG convention through :func:`mcuhome.model.userpaths.config_dir`
    on POSIX, ``%APPDATA%\\mcuhome`` on Windows. An environment that
    names no home at all (a service account, say) simply has no user
    layer — for *reading configuration* that is an empty layer, not an
    error, unlike the signing key's refusal in the same situation.
    """
    if os.name == "nt":
        base = env.get("APPDATA")
        return Path(base) / "mcuhome" if base else None
    try:
        return config_dir(dict(env))
    except ConfigError:
        return None


def _key_location(data: Any, key: str, file: Path) -> Location:
    """Where *key* sits in *file*, from ruamel's round-trip bookkeeping."""
    try:
        line, column = data.lc.key(key)
        return Location(file=file, line=line + 1, column=column + 1, key=key)
    except (AttributeError, KeyError, TypeError):
        return Location(file=file, key=key)


def _parse_file_value(
    opt: Option, value: Any, *, file: Path, env: Mapping[str, str], location: Location
) -> Any:
    def refuse(expected: str) -> ConfigError:
        return ConfigError(
            f"The option {opt.name!r} must be {expected}.",
            location=location,
            hint=opt.help or None,
        )

    if opt.kind == "string":
        if not isinstance(value, str):
            raise refuse("a string")
        return value
    if opt.kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise refuse("a whole number")
        return value
    if opt.kind == "path":
        if not isinstance(value, str) or not value:
            raise refuse("a path")
        return _resolve_path(value, env=env, base=file.parent)
    if opt.kind == "paths":
        if isinstance(value, str):
            raise refuse("a list of paths (one `- path` line each), not a single string")
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise refuse("a list of paths")
        return tuple(_resolve_path(item, env=env, base=file.parent) for item in value)
    raise ValueError(f"option {opt.name!r} declares unknown kind {opt.kind!r}")


def _resolve_path(value: str, *, env: Mapping[str, str], base: Path | None) -> Path:
    path = expand(value, dict(env))
    if base is not None and not path.is_absolute():
        # A relative path in a configuration file is relative to that
        # file, not to wherever the reading process happens to stand —
        # the file cannot know the latter and its author can see the
        # former.
        path = (base / path).resolve()
    return path


def _parse_env_value(opt: Option, value: str, env: Mapping[str, str]) -> Any:
    if opt.kind == "string":
        return value
    if opt.kind == "integer":
        try:
            return int(value)
        except ValueError:
            raise ConfigError(
                f"{opt.env_var} must be a whole number, not {value!r}.",
                hint=opt.help or None,
            ) from None
    if opt.kind == "path":
        return _resolve_path(value, env=env, base=None)
    if opt.kind == "paths":
        return tuple(
            _resolve_path(item, env=env, base=None) for item in value.split(os.pathsep) if item
        )
    raise ValueError(f"option {opt.name!r} declares unknown kind {opt.kind!r}")


def _read_layer(
    file: Path,
    *,
    origin: str,
    registry: tuple[Option, ...],
    env: Mapping[str, str],
) -> dict[str, Setting]:
    if not file.is_file():
        return {}
    data = load_yaml_file(file)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(
            f"{file.name} must be a mapping of `option: value` pairs.",
            location=Location(file=file, line=1, column=1),
            hint="one option per line, for example:\n    jobs: 4",
        )
    by_name = {opt.name: opt for opt in registry}
    settable = sorted(name for name, opt in by_name.items() if opt.files and not opt.bootstrap)
    settings: dict[str, Setting] = {}
    for key, raw in data.items():
        location = _key_location(data, str(key), file)
        if key not in by_name:
            raise ConfigError(
                f"There is no option called {key!r}.",
                location=location,
                hint="options settable from a configuration file: " + ", ".join(settable),
            )
        opt = by_name[key]
        if opt.bootstrap:
            raise ConfigError(
                f"{opt.name!r} cannot be set from a configuration file.",
                location=location,
                hint=(
                    f"{opt.name!r} decides where the project layer *is*, so it runs "
                    f"before any configuration file is read (ADR 0022 §2). Set it per "
                    f"invocation: {opt.flag} on the command line, or {opt.env_var} in "
                    "the environment."
                ),
            )
        if not opt.files:
            raise ConfigError(
                f"{opt.name!r} cannot be set from a configuration file.",
                location=location,
                hint=(
                    f"{opt.name!r} is a per-invocation value: set it with {opt.flag} "
                    f"on the command line or as {opt.env_var} in the environment."
                ),
            )
        value = _parse_file_value(opt, raw, file=file, env=env, location=location)
        settings[opt.name] = Setting(option=opt, value=value, origin=origin, source=str(file))
    return settings


def resolve_settings(
    *,
    project: Project | None,
    env: Mapping[str, str],
    args: Mapping[str, Any] | None = None,
    registry: tuple[Option, ...] = OPTIONS,
) -> Settings:
    """Resolve *registry* through the five layers of ADR 0022 §2.

    *project* is the already-resolved project directory (or ``None``
    outside any project — the project layer is then simply absent), and
    *env* the stated environment both the ``MCUHOME_*`` layer and the
    file locations are read from. *args* carries the invocation's
    values, keyed by option name and holding **only what the caller was
    actually given** — an unset flag must be absent here, not ``None``,
    because "the flag was not used" and "the flag was used to clear the
    option" are different statements and only the caller can tell them
    apart.

    The bootstrap options are skipped: they were consumed before this
    ran (:func:`mcuhome.workbench.project.resolve_project`), and a file
    that tries to set one is refused with the reason.
    """
    resolved: dict[str, Setting] = {
        opt.name: Setting(option=opt, value=opt.default, origin="default")
        for opt in registry
        if not opt.bootstrap
    }

    layers: list[tuple[str, Path | None]] = [
        ("system", system_config_dir(env)),
        ("user", user_config_dir(env)),
    ]
    for origin, directory in layers:
        if directory is None:
            continue
        resolved.update(
            _read_layer(directory / CONFIG_FILE, origin=origin, registry=registry, env=env)
        )
    if project is not None:
        resolved.update(
            _read_layer(project.config_file, origin="project", registry=registry, env=env)
        )

    for opt in registry:
        if opt.bootstrap or not opt.environment:
            continue
        raw = env.get(opt.env_var)
        if raw is None or raw == "":
            continue
        resolved[opt.name] = Setting(
            option=opt,
            value=_parse_env_value(opt, raw, env),
            origin="environment",
            source=opt.env_var,
        )

    for name, value in (args or {}).items():
        opt = option(name, registry)
        if opt.bootstrap:
            raise ValueError(f"{name!r} is a bootstrap option; resolve_project consumed it already")
        if not opt.arguments:
            raise ValueError(f"{name!r} is not settable from the command line")
        resolved[name] = Setting(option=opt, value=value, origin="arguments", source=opt.flag)

    return Settings(resolved)
