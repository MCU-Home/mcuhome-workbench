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
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcuhome.model.errors import ConfigError, Location
from mcuhome.model.userpaths import config_dir, expand

from mcuhome.workbench import builders as builders_module
from mcuhome.workbench.builders import CREDENTIALS_TOKEN_KEY, Builder, SelectedBuilder
from mcuhome.workbench.loader import FileRef, editing_yaml, load_yaml_file
from mcuhome.workbench.project import Project, check_secret_file

__all__ = [
    "CONFIG_FILE",
    "CONFIG_SCOPES",
    "OPTIONS",
    "Option",
    "Setting",
    "Settings",
    "option",
    "resolve_builder",
    "resolve_settings",
    "scope_config_file",
    "set_config_value",
    "system_config_dir",
    "unset_config_value",
    "user_config_dir",
]

#: File name of the system and user layers. Deliberately not
#: ``mcuhome.yaml`` — the module docstring says why.
CONFIG_FILE = "configuration.yaml"

#: The three file scopes ``mcuhome config set``/``unset`` write, in the
#: layer order of the model. The other two layers have no file to edit:
#: the environment is the shell's, the arguments are the invocation's.
CONFIG_SCOPES = ("system", "user", "project")

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
        help="the project directory; disables the upward marker search",
    ),
    Option(
        "sdk_sources",
        kind="paths",
        default=(),
        help="directories holding hash-pinned MCUHome SDK packages",
    ),
    Option(
        "signing_key",
        kind="path",
        files=False,
        help="a firmware signing key file to use instead of the project's",
    ),
    Option(
        "jobs",
        kind="integer",
        default=1,
        help="parallel compile jobs a build may use",
    ),
    # One cache for everything this user builds — its entries are content
    # addresses, so two projects share one exactly when the compilation
    # is the same compilation. Left unset it lands under the user's cache
    # directory; setting it moves the cache to a faster disk, or off a
    # network home directory.
    Option(
        "ccache_dir",
        kind="path",
        help="where the compiler cache lives; unset means the user cache directory",
    ),
    # ADR 0023: builders are deployment configuration and live in files
    # only — the fully manual rung (--build-mode plus its flags) is the
    # per-invocation channel and bypasses the list entirely.
    Option(
        "builders",
        kind="builders",
        default=(),
        environment=False,
        arguments=False,
        help="named builders: where a build may run",
    ),
    # Settable up to the environment; the *invocation* selects with
    # --builder, which is selection rather than configuration — so the
    # arguments channel is deliberately off here.
    Option(
        "default_builder",
        kind="string",
        arguments=False,
        help="the builder a plain `mcuhome device build` uses",
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
            if isinstance(value, Builder):
                return value.to_dict()
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
    opt: Option,
    value: Any,
    *,
    file: Path,
    env: Mapping[str, str],
    location: Location,
    origin: str,
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
    if opt.kind == "builders":
        return builders_module.parse_builders(value, file=file, origin=origin)
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


def _refuse_not_file_settable(opt: Option, location: Location | None) -> ConfigError:
    """The channel refusal of ADR 0022 §3, for reading and writing alike."""
    if opt.bootstrap:
        return ConfigError(
            f"{opt.name!r} cannot be set from a configuration file.",
            location=location,
            hint=(
                f"{opt.name!r} decides where the project layer *is*, so it runs "
                f"before any configuration file is read. Set it per "
                f"invocation: {opt.flag} on the command line, or {opt.env_var} in "
                "the environment."
            ),
        )
    return ConfigError(
        f"{opt.name!r} cannot be set from a configuration file.",
        location=location,
        hint=(
            f"{opt.name!r} is a per-invocation value: set it with {opt.flag} "
            f"on the command line or as {opt.env_var} in the environment."
        ),
    )


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
        if opt.bootstrap or not opt.files:
            raise _refuse_not_file_settable(opt, location)
        value = _parse_file_value(opt, raw, file=file, env=env, location=location, origin=origin)
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

    def apply(layer_settings: dict[str, Setting]) -> None:
        # Scalars are whole-value nearest-wins; builder lists merge by
        # name across the layers (ADR 0023 §3), so a machine can ship
        # site builders, a user can add their own, and a project can
        # pin its default without any layer repeating the others.
        for name, setting in layer_settings.items():
            below = resolved.get(name)
            if setting.option.kind == "builders" and below is not None and below.value:
                setting = Setting(
                    option=setting.option,
                    value=builders_module.merge_builders(below.value, setting.value),
                    origin=setting.origin,
                    source=setting.source,
                )
            resolved[name] = setting

    layers: list[tuple[str, Path | None]] = [
        ("system", system_config_dir(env)),
        ("user", user_config_dir(env)),
    ]
    for origin, directory in layers:
        if directory is None:
            continue
        apply(_read_layer(directory / CONFIG_FILE, origin=origin, registry=registry, env=env))
    if project is not None:
        apply(_read_layer(project.config_file, origin="project", registry=registry, env=env))

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


# --------------------------------------------------------------------------
# Builder selection (ADR 0023 §2/§4)
# --------------------------------------------------------------------------


def resolve_builder(
    settings: Settings,
    *,
    name: str | None = None,
    project: Project | None,
    env: Mapping[str, str],
    on_warning: Callable[[str], None] | None = None,
) -> SelectedBuilder:
    """Which builder this invocation uses, credentials included.

    The two configured rungs of ADR 0023 §2 — an explicit ``--builder``
    *name*, then the configured ``default_builder`` — over the resolved
    ``builders`` list, falling back to a plain ``local`` build when
    neither is set. The fully manual rung never calls this. A remote
    builder's token comes from ``secrets/build-server/<name>.yaml``,
    looked up nearest-first: the project, then the user configuration
    directory, then the system one — the same ladder its definition
    merged through, and the **nearest existing file answers whole**
    (a project file without a ``token`` key means "no token", it does
    not fall through to the user's). A missing file everywhere is a
    tokenless builder, which is permitted — a third-party server may
    want no Authorization at all.
    """
    return builders_module.select_builder(
        settings.value("builders"),
        name=name,
        default=settings.value("default_builder"),
        token_of=lambda builder: _builder_token(
            builder.name, project=project, env=env, on_warning=on_warning
        ),
    )


def _builder_token(
    name: str,
    *,
    project: Project | None,
    env: Mapping[str, str],
    on_warning: Callable[[str], None] | None,
) -> str | None:
    relative = Path("build-server") / f"{name}.yaml"
    candidates: list[Path] = []
    if project is not None:
        candidates.append(project.secrets_dir / relative)
    for directory in (user_config_dir(env), system_config_dir(env)):
        if directory is not None:
            candidates.append(directory / "secrets" / relative)
    for file in candidates:
        if not file.is_file():
            continue
        check_secret_file(file, key_material=False, on_warning=on_warning)
        data = load_yaml_file(file)
        if data is None:
            return None
        if not isinstance(data, dict):
            raise ConfigError(
                f"{file} must be a mapping of `name: value` pairs.",
                location=Location(file=file, line=1, column=1),
                hint=f"the token of the builder {name!r} goes on one line:\n    token: <token>",
            )
        token = data.get(CREDENTIALS_TOKEN_KEY)
        if token is None:
            # The file may legitimately carry only future material
            # (TLS pinning, certificates — ADR 0023 §4); an unknown
            # key is the future, not a typo worth refusing.
            return None
        if isinstance(token, FileRef):
            # `token: !file <name>`: the referenced file is the secret,
            # so it gets the same §5 check as this one — and the old
            # token-file rule (E63) holds for its content: surrounding
            # whitespace is an editor's newline, whitespace inside is a
            # file with something else in it.
            check_secret_file(token.path, key_material=False, on_warning=on_warning)
            bare = token.strip()
            if not bare or any(character.isspace() for character in bare):
                raise ConfigError(
                    f"The file behind {CREDENTIALS_TOKEN_KEY} in {file} does not hold "
                    "a bare token.",
                    location=Location(file=file, key=CREDENTIALS_TOKEN_KEY),
                    hint=(
                        f"{token.path} must hold the bearer token and nothing else — "
                        "a trailing newline is fine and ignored"
                    ),
                )
            return bare
        if not isinstance(token, str):
            raise ConfigError(
                f"The {CREDENTIALS_TOKEN_KEY} in {file} must be a string.",
                location=Location(file=file, key=CREDENTIALS_TOKEN_KEY),
                hint='quote it if it looks like a number: token: "12345"',
            )
        return token
    return None


# --------------------------------------------------------------------------
# Writing configuration (`mcuhome config set`/`unset`, ADR 0022 §3)
# --------------------------------------------------------------------------


def scope_config_file(
    scope: str,
    *,
    project: Project | None,
    env: Mapping[str, str],
) -> Path:
    """The file a configuration scope is edited in.

    The write-side counterpart of the three file layers: ``project`` is
    the project's ``mcuhome.yaml``, ``user`` and ``system`` are their
    directories' ``configuration.yaml``. Reading treats an unnameable
    directory as an absent layer; *editing* one is a refusal instead —
    a value written into a layer that cannot exist would silently
    configure nothing.
    """
    if scope == "project":
        if project is None:
            raise ConfigError(
                "There is no project here to configure.",
                hint=(
                    "the project scope writes mcuhome.yaml in the project directory "
                    "(the upward marker search found none). Run `mcuhome project init` first, "
                    "or write the user/system configuration instead."
                ),
            )
        return project.config_file
    if scope not in CONFIG_SCOPES:
        raise ValueError(f"{scope!r} is not a configuration scope")
    directory = user_config_dir(env) if scope == "user" else system_config_dir(env)
    if directory is None:
        raise ConfigError(
            f"This environment names no {scope} configuration directory.",
            hint=(
                "the user directory follows XDG_CONFIG_HOME/HOME (POSIX) or %APPDATA% "
                "(Windows); the system directory is /etc/mcuhome or %ProgramData%\\mcuhome"
            ),
        )
    return directory / CONFIG_FILE


def _declared_or_refuse(name: str, registry: tuple[Option, ...]) -> Option:
    by_name = {opt.name: opt for opt in registry}
    if name not in by_name:
        settable = sorted(n for n, o in by_name.items() if o.files and not o.bootstrap)
        raise ConfigError(
            f"There is no option called {name!r}.",
            hint="options settable from a configuration file: " + ", ".join(settable),
        )
    return by_name[name]


def _value_to_write(opt: Option, text: str, location: Location) -> Any:
    """What ``config set`` puts into the file, validated but unresolved.

    The user's own spelling is written, never a resolution of it: a
    relative path stays relative (the file's rule resolves it on every
    read), a ``paths`` value splits ``os.pathsep``-style — the same
    convention its environment variable uses — into a YAML list.
    """
    if not text:
        raise ConfigError(
            f"An empty value does not set {opt.name!r}.",
            location=location,
            hint=f"to remove the option from the file: mcuhome config unset {opt.name}",
        )
    if opt.kind == "builders":
        raise ConfigError(
            "'builders' is structured configuration and not settable as one value.",
            location=location,
            hint=(
                "edit the `builders:` list in the file directly — one entry per "
                "builder with name:, type: and the type's options"
            ),
        )
    if opt.kind == "integer":
        try:
            return int(text)
        except ValueError:
            raise ConfigError(
                f"{opt.name} must be a whole number, not {text!r}.",
                location=location,
                hint=opt.help or None,
            ) from None
    if opt.kind == "paths":
        return [item for item in text.split(os.pathsep) if item]
    return text


def _load_for_editing(file: Path, yaml: Any) -> Any:
    if not file.is_file():
        return None
    data = yaml.load(file.read_text(encoding="utf-8"))
    if data is not None and not isinstance(data, dict):
        raise ConfigError(
            f"{file.name} must be a mapping of `option: value` pairs.",
            location=Location(file=file, line=1, column=1),
            hint="one option per line, for example:\n    jobs: 4",
        )
    return data


def _dump_config(file: Path, data: Any, yaml: Any) -> None:
    try:
        file.parent.mkdir(parents=True, exist_ok=True)
        with file.open("w", encoding="utf-8") as handle:
            yaml.dump(data, handle)
    except OSError as error:
        raise ConfigError(
            f"MCUHome cannot write {file}: {error.strerror}.",
            hint=(
                "the system scope usually needs administrator rights"
                if str(file).startswith("/etc/") or "ProgramData" in str(file)
                else "pick a scope you can write to"
            ),
        ) from error


def set_config_value(
    file: Path,
    name: str,
    text: str,
    *,
    env: Mapping[str, str],
    registry: tuple[Option, ...] = OPTIONS,
) -> Any:
    """Set *name* to *text* in *file*, and answer with the written value.

    The write obeys the option's channels exactly as reading does — a
    per-invocation or bootstrap option is refused with the same words —
    and goes through the round-trip editor, so comments and ``!file``
    references elsewhere in the file survive the edit byte for byte.
    """
    opt = _declared_or_refuse(name, registry)
    location = Location(file=file, key=name)
    if opt.bootstrap or not opt.files:
        raise _refuse_not_file_settable(opt, location)
    value = _value_to_write(opt, text, location)
    # Prove the written form reads back as a value of the option's kind
    # before anything touches the file — the one guarantee `config set`
    # owes: it never leaves a file behind that the next resolve refuses.
    _parse_file_value(opt, value, file=file, env=env, location=location, origin="edit")
    yaml = editing_yaml()
    data = _load_for_editing(file, yaml)
    if data is None:
        data = {}
    data[name] = value
    _dump_config(file, data, yaml)
    return value


def unset_config_value(
    file: Path,
    name: str,
    *,
    registry: tuple[Option, ...] = OPTIONS,
) -> bool:
    """Remove *name* from *file*; False when there was nothing to remove.

    The name must be a declared option — ``unset`` with a typo saying
    "nothing to remove" would confirm a removal that never happened.
    """
    _declared_or_refuse(name, registry)
    yaml = editing_yaml()
    data = _load_for_editing(file, yaml)
    if data is None or name not in data:
        return False
    del data[name]
    _dump_config(file, data, yaml)
    return True
