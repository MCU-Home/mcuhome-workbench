# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The configuration model: one option registry, and the layers over it.

Every option is declared exactly once, in :data:`OPTIONS` — name, type,
default, and **which channels may set it** — and the ``MCUHOME_*``
variable, the command-line flag and the configuration key all derive
from that declaration. Nothing in this package reads a variable the
registry did not declare: an ad-hoc one is a spelling nobody can find
and a value ``mcuhome config print`` cannot show. Not every option
belongs in every channel: a per-invocation value is
argument+environment only and never lives in a static file, and the one
bootstrap option stands outside the merge entirely
(:mod:`mcuhome.workbench.project` resolves it first, because it decides
where the project layer even is).

The layers, ascending — later wins::

    program      what an embedding program defaults shared keys to
    system       /etc/mcuhome/configuration.yaml (or XDG_CONFIG_DIRS')
    user         $XDG_CONFIG_HOME/mcuhome/configuration.yaml
    project      mcuhome.yaml in the project directory
    environment  MCUHOME_* variables
    command      the invocation's arguments

The **program** layer sits directly above the declared defaults and
below every file: a build server wants ``build.memory`` bounded where a
workstation does not, and a default nobody can see is a value nobody can
account for — so it is a layer with an origin and a source like every
other, and an operator's file still wins.

The system/user files are deliberately **not** named ``mcuhome.yaml``: a
configuration directory is already named ``mcuhome/``, so a file of that
name in it would say nothing, while in a user's own repository — full of
files belonging to other tools — the product name is exactly what the
file has to carry. The directories follow
the platformdirs conventions, computed here from the
*stated* environment rather than through the platformdirs library,
because that library answers out of the process environment and this
package serves several sessions from one process
(:mod:`mcuhome.model.userpaths`). On Windows the conventional homes are
``%ProgramData%\\mcuhome`` and ``%APPDATA%\\mcuhome``, from the stated
environment too; a layer whose directory the environment cannot name
simply does not exist for that resolution.

Merge semantics: scalars are nearest-wins, whole value per layer.
Structured values define their own rule where they are introduced, and
both of the ones that exist merge by the name their entries are keyed on
rather than replacing each other wholesale: builders by builder name,
package registries by base domain. ``mcuhome config print`` falls out of
the same registry: :meth:`Settings.to_dict` answers with every effective
value, the layer it came from and the file, variable or flag inside it.

**Areas.** Every option's name states the area it belongs to, separated
by a dot: ``build.mode``, ``signing.key``. The dot is a real level
everywhere the option is written down — the file nests them under the
area, the environment variable joins them with an underscore
(``MCUHOME_BUILD_MODE``), the flag with a dash (``--build-mode``) — so
one option name produces every spelling::

    build:
      mode: subprocess
      env_store: /var/cache/mcuhome/build-environments

An area may hold a **map** instead of leaves, keyed by a name the user
chooses: ``builder`` by builder name, ``registry`` by base domain. Such
an option is declared under the area name alone, and what follows the
area is data rather than further levels of this scheme — which is also
why the two live in files only.

A derived flag splits back into its key without a table, because an area
name is always one word: ``--build-sdk-sources`` is ``build`` and
``sdk_sources``, and a message may therefore offer either spelling of a
value. A flag exists exactly where the command line is a channel at all:
selecting a builder for one invocation is ``--builder``, which carries a
call's parameter rather than ``build.builder``.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcuhome.model.errors import ConfigError, Location
from mcuhome.model.userpaths import config_dir, expand

from mcuhome.workbench import builders as builders_module
from mcuhome.workbench import packageregistry
from mcuhome.workbench.buildenvstore import (
    EXTRACTION_BOUNDS,
    SDK_KIND,
    TOOLS_KIND,
    WORKSPACE_KIND,
)
from mcuhome.workbench.builders import CREDENTIALS_TOKEN_KEY, SelectedBuilder
from mcuhome.workbench.buildtarget import (
    BUILD_MODES,
    BUILD_TARGETS,
    DEFAULT_BUILD_MODE,
    DEFAULT_BUILD_TARGET,
    DEFAULT_CONTAINER_PROGRAM,
    DEFAULT_CONTAINER_REPOSITORIES,
)
from mcuhome.workbench.loader import FileRef, editing_yaml, read_yaml_file
from mcuhome.workbench.project import BUILDER_SECRETS_DIR, Project, check_secret_file

__all__ = [
    "CONFIG_FILE",
    "CONFIG_SCOPES",
    "OPTION_KINDS",
    "Argument",
    "ProgramDefaults",
    "OPTIONS",
    "Option",
    "Setting",
    "Settings",
    "option",
    "resolve_builder",
    "resolve_config_file",
    "resolve_settings",
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

#: What separates the entries of a ``strings`` option outside a file —
#: in an environment variable and in ``mcuhome config set``. A comma
#: rather than ``os.pathsep``, because the values it separates are names
#: that may contain a colon.
_LIST_SEPARATOR = ","

#: Origin labels, in ascending precedence. ``default`` is what a value
#: has when no layer set it, and ``program`` what an embedding program
#: states for a shared key before any file is read.
_ORIGINS = (
    "default",
    "program",
    "system",
    "user",
    "project",
    "environment",
    "arguments",
)

#: Every kind an option may declare. The two map kinds parse and merge
#: themselves and are named after the area they hold.
OPTION_KINDS = (
    "string",
    "path",
    "paths",
    "strings",
    "integer",
    "number",
    "builder",
    "registry",
)


@dataclass(frozen=True)
class Option:
    """One declared option — the single source of every spelling.

    *kind* is one of :data:`OPTION_KINDS`: ``string``, ``path``,
    ``paths`` (an ordered list, ``os.pathsep``-separated in the
    environment), ``strings`` (an ordered list of plain names,
    comma-separated in the environment because the names may contain a
    colon), ``integer``, ``number``, and the two map kinds ``builder``
    and ``registry``, which parse and merge themselves and live in files
    only.
    The three channel switches say where the option may be set:
    *files* covers all three file layers at once — there is no option
    that a user file may set and a system file may not. *bootstrap*
    marks the option that runs before the merge; it is declared here so
    its spellings derive like everyone else's, but
    :func:`resolve_settings` refuses it from files and skips it in the
    merge (:func:`mcuhome.workbench.project.resolve_project` is its
    resolver).

    *choices* and *minimum* are the declaration's own validation, and
    they are here rather than in each reader for the reason the rest of
    this class exists: a value is checked once, in the layer that
    supplied it, so a refusal can name the file and the line — and every
    channel is held to the same rule without three of them agreeing to.
    """

    name: str
    kind: str
    default: Any = None
    files: bool = True
    environment: bool = True
    arguments: bool = True
    bootstrap: bool = False
    help: str = ""
    #: The values a ``string`` option accepts, if it is a vocabulary
    #: rather than free text. Empty means free text.
    choices: tuple[str, ...] = ()
    #: The smallest value an ``integer`` option accepts, and the value a
    #: ``number`` option has to be strictly greater than — half a core is
    #: a share, zero cores is not. ``None`` means any.
    minimum: int | None = None

    @property
    def area(self) -> str:
        """The area this option belongs to — the part before the dot.

        Every option has one. A map option *is* its area (``builder``,
        ``registry``): it is declared once, under the area name alone,
        and what a user writes below it are names of their own choosing
        rather than further levels of this scheme.
        """
        return self.name.partition(".")[0]

    @property
    def leaf(self) -> str:
        """The name inside the area, or empty for an option that is its area.

        The key a configuration file writes below the area's section —
        so a map option has none: the whole section is its value.
        """
        return self.name.partition(".")[2]

    @property
    def env_var(self) -> str:
        """``MCUHOME_<AREA>_<NAME>``, or empty where the channel is closed.

        Derived from the key and nothing else, so one declaration
        produces every spelling. Empty exactly when the environment
        cannot set this option, which is what a message checks before
        offering the variable.
        """
        if not self.environment:
            return ""
        return "MCUHOME_" + self.name.upper().replace(".", "_")

    @property
    def flag(self) -> str:
        """``--<key with dots and underscores as dashes>``, or empty.

        Total and reversible: an area name is one word, so
        ``--build-sdk-sources`` splits back into ``build`` and
        ``sdk_sources`` without a table. A message may therefore offer
        either spelling of a value, and a value that arrived through the
        arguments channel can name the flag it came from.

        Empty exactly when the command line cannot set this option —
        selecting a builder for one invocation is ``--builder``, which
        carries a call's parameter rather than this key, and a map
        option is written in a file or not at all.
        """
        if not self.arguments:
            return ""
        return "--" + self.name.replace(".", "-").replace("_", "-")


#: The platform's option registry. Tools may resolve additional
#: registries of their own through the same machinery (the CLI's
#: presentation options, say) — these are the options the *platform*
#: owns, shared by every tool.
#:
#: **Declared default against derived fallback.** What stands in
#: ``default`` here is what ``mcuhome config print`` shows with the
#: origin ``default``. What a consumer does with a value nobody set —
#: the user's cache directory, the interpreter this process runs on, the
#: machine's own cores — is *not* written here: a key carrying its
#: consumer's fallback would look configured when it is not, and the
#: fallback would then be in two places at once.
OPTIONS: tuple[Option, ...] = (
    Option(
        "project.dir",
        kind="path",
        files=False,
        bootstrap=True,
        help="the project directory; disables the upward marker search",
    ),
    Option(
        "signing.key",
        kind="path",
        files=False,
        help="a firmware signing key file to use instead of the project's",
    ),
    # A name or a path, and looked up like any other program when it is
    # a name. The escape hatch for a machine where the installed package
    # is not the right imgtool.
    Option(
        "signing.imgtool",
        kind="string",
        help="the imgtool that signs firmware; unset uses the installed one",
    ),
    # -- build.* : how this machine builds -----------------------------
    # Everything below describes the machine a build runs on, not the
    # firmware: where a build of it runs, which of the two executions it
    # uses, where the unpacked build environment lives, and what it may
    # spend on it. All of it is a property of the host, so all of it is
    # configuration and none of it belongs in a device.
    #
    # The two axes are two keys, deliberately: `build.target` is where a
    # build runs and `build.mode` is how the machine that runs it
    # executes the work — a client does not get to tell a build server
    # whether to start a container, so one word for both could never
    # stay symmetric.
    Option(
        "build.target",
        kind="string",
        default=DEFAULT_BUILD_TARGET,
        choices=BUILD_TARGETS,
        help="where a build runs: on this machine, or on a build server",
    ),
    Option(
        "build.mode",
        kind="string",
        default=DEFAULT_BUILD_MODE,
        choices=BUILD_MODES,
        help="how a local build is executed: in a build container, or as a child process",
    ),
    # Settable up to the environment; the *invocation* selects with
    # --builder, which is selection rather than configuration — so the
    # arguments channel is deliberately off here and this key derives no
    # flag.
    Option(
        "build.builder",
        kind="string",
        arguments=False,
        help="the builder a plain `mcuhome device build` uses",
    ),
    # `podman` is command-line compatible for everything used here,
    # which is the whole reason this key exists.
    Option(
        "build.container_program",
        kind="string",
        default=DEFAULT_CONTAINER_PROGRAM,
        help="the program that runs build containers",
    ),
    # Where a container build may take its environment from, in search
    # order. An image is chosen by the packages its labels declare, never
    # by its name, so this list is not "which image" but "whose images
    # may be trusted to deliver one" — which is why it is configuration
    # and not a device's business.
    Option(
        "build.container_repositories",
        kind="strings",
        default=DEFAULT_CONTAINER_REPOSITORIES,
        help="container repositories a build environment may be taken from, in order",
    ),
    # What one build may use of this machine. The container profile sets
    # them on the container and enforces them; both profiles state them
    # in the request document, where the build environment reads what it
    # should size itself to. Unset means the machine as it is — a local
    # build is not a tenant, and the guard exists against an environment
    # that runs amok rather than against the person who started it.
    Option(
        "build.cpus",
        kind="number",
        minimum=0,
        help="how much CPU one build may use, in cores; unset means all of them",
    ),
    Option(
        "build.memory",
        kind="string",
        help="how much memory one build may use (512m, 8g, or bytes); unset means what is free",
    ),
    Option(
        "build.env_store",
        kind="path",
        help="where unpacked build environments are kept; unset means the user cache directory",
    ),
    # A development build: one setting, because what it names is a whole
    # environment. The workspace carries the sources, its manifest
    # repository is the SDK that gets compiled, and the tools are the
    # ones on the PATH the build was started from.
    Option(
        "build.dev_workspace",
        kind="path",
        help="a west workspace of your own to build against instead of a provisioned one",
    ),
    # A name, not a path option: `python3.13` is what an operator writes
    # and it is looked up on PATH like any other program, while a path
    # option would resolve that name against the configuration file's
    # own directory and produce a file nobody has.
    Option(
        "build.python",
        kind="string",
        help="the Python that creates a build environment's virtual environment",
    ),
    # One key per package kind, and a kind is never looked for under
    # another kind's key: a directory that holds the SDK package is not
    # thereby a claim about where build workspaces live, and a machine
    # that keeps everything in one place says so three times — which is
    # the statement it is actually making.
    Option(
        "build.sdk_sources",
        kind="paths",
        default=(),
        help="directories holding hash-pinned MCUHome SDK packages",
    ),
    Option(
        "build.workspace_sources",
        kind="paths",
        default=(),
        help="directories holding build workspace packages",
    ),
    Option(
        "build.tools_sources",
        kind="paths",
        default=(),
        help="directories holding build tools packages",
    ),
    # The unpacking bounds. Not a tuning knob for speed: a package is
    # trusted by its pinned hash before a byte of it is unpacked, and the
    # bound is what turns a corrupt or hostile archive into a bounded
    # read instead of a full disk. They are overridable because somebody
    # else's workspace package is legitimately a much larger thing than
    # MCUHome's own.
    Option(
        "build.sdk_max_bytes",
        kind="integer",
        default=EXTRACTION_BOUNDS[SDK_KIND],
        minimum=1,
        help="how much the SDK package may unpack to, in bytes",
    ),
    Option(
        "build.workspace_max_bytes",
        kind="integer",
        default=EXTRACTION_BOUNDS[WORKSPACE_KIND],
        minimum=1,
        help="how much the build workspace package may unpack to, in bytes",
    ),
    Option(
        "build.tools_max_bytes",
        kind="integer",
        default=EXTRACTION_BOUNDS[TOOLS_KIND],
        minimum=1,
        help="how much the build tools package may unpack to, in bytes",
    ),
    # One cache for everything this user builds — its entries are content
    # addresses, so two projects share one exactly when the compilation
    # is the same compilation. Left unset it lands under the user's cache
    # directory; setting it moves the cache to a faster disk, or off a
    # network home directory. The four tiers below name a tier's
    # directory outright, for a machine that keeps one somewhere else —
    # a shared cache on a read-only mount, a project-wide cache on a
    # fast disk.
    Option(
        "build.cache_root",
        kind="path",
        help="where the compiler cache lives; unset means the user cache directory",
    ),
    Option(
        "build.cache_local",
        kind="path",
        help="this machine's own compiler cache; unset uses the cache root",
    ),
    Option(
        "build.cache_shared",
        kind="path",
        help="a compiler cache shared with other machines, read-only to a build",
    ),
    Option(
        "build.cache_session",
        kind="path",
        help="a compiler cache kept for one build session; unset means no session tier",
    ),
    Option(
        "build.cache_project",
        kind="path",
        help="a compiler cache kept for one project; unset means no project tier",
    ),
    # Builders are deployment configuration and live in files only — the
    # fully manual rung (--build-mode plus its flags) is the
    # per-invocation channel and bypasses the map entirely. `builder` is
    # also a reserved area: no option in it ever reads the environment,
    # because MCUHOME_BUILDER_* is what MCUHome sets *for* a build
    # environment it starts.
    Option(
        "builder",
        kind="builder",
        default=(),
        environment=False,
        arguments=False,
        help="named builders, by name: where a build may run",
    ),
    # Package registries, by base domain. A nested map, so it is a file
    # option like `builder`: an environment variable spelling of
    # `registry.<domain>.mirrors.<source>` would be a second grammar to
    # specify and parse for a value nobody sets per invocation.
    Option(
        "registry",
        kind="registry",
        default=(),
        environment=False,
        arguments=False,
        help="package registries by domain: their mirrors, and whether they are trusted",
    ),
)


def option(name: str, declared_options: tuple[Option, ...] = OPTIONS) -> Option:
    """The declaration of *name*, or a ``ValueError`` for a name nobody declared.

    A programming error rather than a refusal in words: a tool asks for
    an option it knows, and a name that reaches here without being one
    came from code, not from a person. What a *person* mistyped is
    refused where it was written — in a file, a variable or a flag.
    """
    for declared in declared_options:
        if declared.name == name:
            return declared
    raise ValueError(f"{name!r} is not a declared option")


@dataclass(frozen=True)
class Argument:
    """One value an invocation carried, in the spelling it arrived in.

    *flag* is what the person actually typed. A tool that offers the
    derived spelling may leave it empty and the declaration answers
    instead; a tool with a flag of its own states it, and a later refusal
    can then quote the words the person used rather than a key they
    never wrote.
    """

    #: The option this sets, by its declared key.
    name: str
    #: The parsed value, in the option's own type — the caller parsed it,
    #: because only the caller knows whether the flag was used at all.
    value: Any
    #: The spelling used, ``--build-mode`` style. Empty takes
    #: :attr:`Option.flag`.
    flag: str = ""


@dataclass(frozen=True)
class ProgramDefaults:
    """What an embedding program defaults shared keys to.

    A build server wants ``build.memory`` bounded where a workstation
    does not, and a default nobody can see is a value nobody can
    account for. So a program states its own values in a layer of its
    own — directly above the registry's defaults and below every file,
    because an operator's file must still win — and every value it sets
    carries the origin ``program`` and this program's *name* as its
    source.
    """

    #: The program, as ``mcuhome config print`` should name it.
    name: str
    #: Its values, keyed by option name and already in the option's type.
    values: Mapping[str, Any]


@dataclass(frozen=True)
class Setting:
    """One resolved value, with the layer it came from."""

    option: Option
    value: Any
    #: One of :data:`_ORIGINS`: ``default``, ``program``, ``system``,
    #: ``user``, ``project``, ``environment``, ``arguments``.
    origin: str
    #: Where exactly: the file for a file layer, the variable name for
    #: the environment, the flag for an argument, the program's name for
    #: its own defaults, ``None`` for a declared default.
    source: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """This value as a document: what it is, and where it came from."""
        return {
            "value": _jsonable(self.value),
            "origin": self.origin,
            "source": self.source,
        }


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

    def to_dict(self) -> dict[str, dict[str, Any]]:
        """Every effective value as one document, in declaration order.

        Declaration order rather than alphabetical, because it groups
        related options the way the registry does. Every value goes
        through its own ``to_dict`` on the way out, so no client ever
        meets a Python object where it asked for data.
        """
        return {name: setting.to_dict() for name, setting in self._settings.items()}


def _jsonable(value: Any) -> Any:
    """One resolved value as JSON-ready data.

    Paths become strings, tuples become lists, and anything that knows
    its own document shape is asked for it — a client that prints
    configuration should never have to recognize a workbench class.
    """
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    return value


def system_config_dir(env: Mapping[str, str]) -> Path | None:
    """The system configuration directory, or None when the platform names none.

    ``/etc/mcuhome`` on POSIX; ``%ProgramData%\\mcuhome`` on Windows.
    ``None`` — rather than an error — because an absent system layer is a
    normal machine, not a broken one.

    **The stated environment decides this layer too.** ``XDG_CONFIG_DIRS``
    is the convention's own name for the system configuration search
    path, and its first entry — the most important one — is where this
    layer lives when the variable is set: ``<first entry>/mcuhome``.
    Unset, the answer is the conventional ``/etc/mcuhome``. So a caller
    that states an environment gets an answer *about that environment*
    rather than about the machine the process happens to run on, which is
    what every other path in MCUHome already promises
    (:mod:`mcuhome.model.userpaths`) — and what lets a test, a container
    or a second session have a system layer of its own instead of the
    one real ``/etc``.
    """
    if os.name == "nt":
        base = env.get("ProgramData")
        return Path(base) / "mcuhome" if base else None
    stated = env.get("XDG_CONFIG_DIRS", "").split(os.pathsep)[0]
    if stated:
        return _resolve_path(stated, env=env, base=None) / "mcuhome"
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
        if opt.choices and value not in opt.choices:
            raise refuse("one of " + ", ".join(opt.choices))
        return value
    if opt.kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise refuse("a whole number")
        if opt.minimum is not None and value < opt.minimum:
            raise refuse(f"at least {opt.minimum}")
        return value
    if opt.kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise refuse("a number")
        if opt.minimum is not None and value <= opt.minimum:
            raise refuse(f"greater than {opt.minimum}")
        return float(value)
    if opt.kind == "path":
        if not isinstance(value, str) or not value:
            raise refuse("a path")
        return _resolve_path(value, env=env, base=file.parent)
    if opt.kind == "strings":
        if isinstance(value, str):
            raise refuse("a list (one `- value` line each), not a single string")
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise refuse("a list of names")
        return tuple(item for item in value if item)
    if opt.kind == "paths":
        if isinstance(value, str):
            raise refuse("a list of paths (one `- path` line each), not a single string")
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise refuse("a list of paths")
        return tuple(_resolve_path(item, env=env, base=file.parent) for item in value)
    if opt.kind == "builder":
        return builders_module.parse_builders(value, file=file, origin=origin)
    if opt.kind == "registry":
        return packageregistry.parse_registries(value, file=file, origin=origin, env=env)
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
        if opt.choices and value not in opt.choices:
            raise ConfigError(
                f"{opt.env_var} must be one of {', '.join(opt.choices)}, not {value!r}.",
                hint=opt.help or None,
            )
        return value
    if opt.kind == "integer":
        try:
            number = int(value)
        except ValueError:
            raise ConfigError(
                f"{opt.env_var} must be a whole number, not {value!r}.",
                hint=opt.help or None,
            ) from None
        if opt.minimum is not None and number < opt.minimum:
            raise ConfigError(
                f"{opt.env_var} must be at least {opt.minimum}, not {number}.",
                hint=opt.help or None,
            )
        return number
    if opt.kind == "number":
        try:
            fraction = float(value)
        except ValueError:
            raise ConfigError(
                f"{opt.env_var} must be a number, not {value!r}.",
                hint=opt.help or None,
            ) from None
        if opt.minimum is not None and fraction <= opt.minimum:
            raise ConfigError(
                f"{opt.env_var} must be greater than {opt.minimum}, not {fraction:g}.",
                hint=opt.help or None,
            )
        return fraction
    if opt.kind == "path":
        return _resolve_path(value, env=env, base=None)
    if opt.kind == "strings":
        # Comma-separated, deliberately not `os.pathsep`: a container
        # repository legitimately carries a colon (a registry port, a
        # tag), and a separator a value can contain is not one.
        return tuple(item.strip() for item in value.split(_LIST_SEPARATOR) if item.strip())
    if opt.kind == "paths":
        return tuple(
            _resolve_path(item, env=env, base=None) for item in value.split(os.pathsep) if item
        )
    raise ValueError(f"option {opt.name!r} declares unknown kind {opt.kind!r}")


#: How a structured option's value from a higher layer combines with the
#: one below it. A kind that is absent here is nearest-wins, whole value.
_MERGERS: dict[str, Callable[[Any, Any], Any]] = {
    "builder": builders_module.merge_builders,
    "registry": packageregistry.merge_registries,
}


def _refuse_not_file_settable(opt: Option, location: Location | None) -> ConfigError:
    """The channel refusal, for reading and writing alike.

    Both texts offer the two per-invocation channels by name. An option
    in an area has no flag to offer (:attr:`Option.flag`), and the
    sentence then names the environment variable alone rather than a
    spelling that does not exist.
    """
    per_invocation = (
        f"{opt.flag} on the command line, or {opt.env_var} in the environment"
        if opt.flag
        else f"{opt.env_var} in the environment"
    )
    if opt.bootstrap:
        return ConfigError(
            f"{opt.name!r} cannot be set from a configuration file.",
            location=location,
            hint=(
                f"{opt.name!r} decides where the project layer *is*, so it runs "
                f"before any configuration file is read. Set it per "
                f"invocation: {per_invocation}."
            ),
        )
    return ConfigError(
        f"{opt.name!r} cannot be set from a configuration file.",
        location=location,
        hint=f"{opt.name!r} is a per-invocation value: set it with {per_invocation}.",
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
    data = read_yaml_file(file)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(
            f"{file.name} must be a mapping of `option: value` pairs.",
            location=Location(file=file, line=1, column=1),
            hint="one area per section, for example:\n    build:\n      builder: attic",
        )
    by_name = {opt.name: opt for opt in registry}
    settable = sorted(name for name, opt in by_name.items() if opt.files and not opt.bootstrap)
    # An area that holds leaves is a section; an area that *is* an option
    # (the map kinds) is read as that option, whole.
    areas = {opt.area for opt in registry if opt.leaf}
    settings: dict[str, Setting] = {}

    def in_area(area: str) -> str:
        inside = sorted(opt.leaf for opt in registry if opt.area == area and opt.files and opt.leaf)
        return ", ".join(inside) if inside else "none — they are set per invocation"

    def read(opt: Option, raw: Any, location: Location) -> None:
        if opt.bootstrap or not opt.files:
            raise _refuse_not_file_settable(opt, location)
        value = _parse_file_value(opt, raw, file=file, env=env, location=location, origin=origin)
        settings[opt.name] = Setting(option=opt, value=value, origin=origin, source=str(file))

    for key, raw in data.items():
        location = _key_location(data, str(key), file)
        declared = by_name.get(key)
        if declared is not None and not declared.leaf:
            # A map option: the section under this key is its value, and
            # the names inside it are the user's own.
            read(declared, raw, location)
            continue
        if key in areas:
            # An area is a block, and its keys are the option names
            # without the area in front of them. Read one level down and
            # nothing further: an option is declared or it is not, and a
            # deeper nesting is somebody's misunderstanding rather than a
            # shape this registry has.
            if not isinstance(raw, dict):
                raise ConfigError(
                    f"The section {key!r} must be a mapping of `option: value` pairs.",
                    location=location,
                    hint=f"its options are: {in_area(key)}",
                )
            for leaf, value in raw.items():
                name = f"{key}.{leaf}"
                inner = _key_location(raw, str(leaf), file)
                if name not in by_name:
                    raise ConfigError(
                        f"There is no option called {name!r}.",
                        location=inner,
                        hint=f"the options of {key} are: {in_area(key)}",
                    )
                read(by_name[name], value, inner)
            continue
        if declared is not None:
            # `build.mode: subprocess` written flat. The option exists,
            # the spelling does not: the dot is a section in a file.
            raise ConfigError(
                f"The option {declared.name!r} is written under its own section here.",
                location=location,
                hint=f"write it as:\n    {declared.area}:\n      {declared.leaf}: <value>",
            )
        raise ConfigError(
            f"There is no option called {key!r}.",
            location=location,
            hint="options settable from a configuration file: " + ", ".join(settable),
        )
    return settings


def resolve_settings(
    *,
    project: Project | None,
    env: Mapping[str, str],
    args: Sequence[Argument] = (),
    program: ProgramDefaults | None = None,
    declared_options: tuple[Option, ...] = OPTIONS,
) -> Settings:
    """Resolve *declared_options* through the layers.

    *project* is the already-resolved project directory (or ``None``
    outside any project — the project layer is then simply absent), and
    *env* the stated environment both the ``MCUHOME_*`` layer and the
    file locations are read from. *args* carries the invocation's values
    and **only what the caller was actually given** — an unused flag is
    absent here, not ``None``, because "the flag was not used" and "the
    flag was used to clear the option" are different statements and only
    the caller can tell them apart. Each one names the spelling it
    arrived in, so a later refusal can quote it.

    *program* is what an embedding program defaults shared keys to; it
    sits directly above the declared defaults and below every file, so
    an operator's configuration still wins.

    The bootstrap option is skipped: it was consumed before this ran
    (:func:`mcuhome.workbench.project.resolve_project`), and a file that
    tries to set it is refused with the reason.
    """
    resolved: dict[str, Setting] = {
        opt.name: Setting(option=opt, value=opt.default, origin="default")
        for opt in declared_options
        if not opt.bootstrap
    }
    for name, value in (program.values if program is not None else {}).items():
        opt = _settable(name, declared_options, channel="a program's own defaults")
        resolved[opt.name] = Setting(
            option=opt, value=value, origin="program", source=program.name if program else None
        )

    def apply(layer_settings: dict[str, Setting]) -> None:
        # Scalars are whole-value nearest-wins; the structured kinds
        # merge by the name their entries are keyed on — the builder
        # name, the registry's base domain — so a machine can ship site
        # entries, a user can add their own, and a project can pin one
        # without any layer having to repeat the others.
        for name, setting in layer_settings.items():
            below = resolved.get(name)
            merge = _MERGERS.get(setting.option.kind)
            if merge is not None and below is not None and below.value:
                setting = Setting(
                    option=setting.option,
                    value=merge(below.value, setting.value),
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
        apply(
            _read_layer(directory / CONFIG_FILE, origin=origin, registry=declared_options, env=env)
        )
    if project is not None:
        apply(
            _read_layer(project.config_file, origin="project", registry=declared_options, env=env)
        )

    for opt in declared_options:
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

    for argument in args:
        opt = _settable(argument.name, declared_options, channel="the command line")
        # The spelling the tool used, or the one this registry derives:
        # either way a refusal can name what the person typed.
        resolved[opt.name] = Setting(
            option=opt,
            value=argument.value,
            origin="arguments",
            source=argument.flag or opt.flag,
        )

    return Settings(resolved)


def _settable(name: str, registry: tuple[Option, ...], *, channel: str) -> Option:
    """The declaration of *name*, or a programming error naming the channel."""
    opt = option(name, registry)
    if opt.bootstrap:
        raise ValueError(f"{name!r} is a bootstrap option; resolve_project consumed it already")
    if channel == "the command line" and not opt.arguments:
        raise ValueError(f"{name!r} is not settable from the command line")
    return opt


# --------------------------------------------------------------------------
# Builder selection
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

    The two configured rungs — an explicit ``--builder`` *name*, then
    the configured ``build.builder`` — over the resolved ``builder`` map,
    falling back to ``build.target`` when neither is set (and that
    key's own default is a build on this machine). A caller that names a
    target outright never calls this. A remote
    builder's token comes from ``secrets/builder/<name>.yaml``,
    looked up nearest-first: the project, then the user configuration
    directory, then the system one — the same ladder its definition
    merged through, and the **nearest existing file answers whole**
    (a project file without a ``token`` key means "no token", it does
    not fall through to the user's). A missing file everywhere is a
    tokenless builder, which is permitted — a third-party server may
    want no Authorization at all.
    """
    return builders_module.select_builder(
        settings.value("builder"),
        name=name,
        default=settings.value("build.builder"),
        fallback=settings.value("build.target"),
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
    relative = Path(BUILDER_SECRETS_DIR) / f"{name}.yaml"
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
        data = read_yaml_file(file)
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
            # (TLS pinning, certificates); an unknown
            # key is the future, not a typo worth refusing.
            return None
        if isinstance(token, FileRef):
            # `token: !file <name>`: the referenced file is the secret,
            # so it gets the same permission check as this one — and the
            # old token-file rule holds for its content: surrounding
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
# Writing configuration (`mcuhome config set`/`unset`)
# --------------------------------------------------------------------------


def resolve_config_file(
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
                "(Windows); the system directory is the first XDG_CONFIG_DIRS entry, "
                "else /etc/mcuhome, or %ProgramData%\\mcuhome"
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
    if opt.kind == "builder":
        raise ConfigError(
            "'builder' is structured configuration and not settable as one value.",
            location=location,
            hint=(
                "edit the `builder:` map in the file directly — one section per "
                "builder, keyed by its name, with target: and what that target needs"
            ),
        )
    if opt.kind == "registry":
        raise ConfigError(
            "'registry' is structured configuration and not settable as one value.",
            location=location,
            hint=(
                "edit the `registry:` block in the file directly — one entry per "
                "registry domain, each with untrusted:, anchor: and mirrors:"
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
    if opt.kind == "number":
        try:
            return float(text)
        except ValueError:
            raise ConfigError(
                f"{opt.name} must be a number, not {text!r}.",
                location=location,
                hint=opt.help or None,
            ) from None
    if opt.kind == "strings":
        return [item.strip() for item in text.split(_LIST_SEPARATOR) if item.strip()]
    if opt.kind == "paths":
        return [item for item in text.split(os.pathsep) if item]
    # A vocabulary is deliberately *not* checked here: the caller proves
    # the written form reads back before it touches the file, and that
    # check is the same one every layer is held to. A second one here
    # would be a second wording of one refusal.
    return text


def _load_for_editing(file: Path, yaml: Any) -> Any:
    if not file.is_file():
        return None
    data = yaml.load(file.read_text(encoding="utf-8"))
    if data is not None and not isinstance(data, dict):
        raise ConfigError(
            f"{file.name} must be a mapping of `option: value` pairs.",
            location=Location(file=file, line=1, column=1),
            hint="one area per section, for example:\n    build:\n      builder: attic",
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
    declared_options: tuple[Option, ...] = OPTIONS,
) -> Any:
    """Set *name* to *text* in *file*, and answer with the written value.

    The write obeys the option's channels exactly as reading does — a
    per-invocation or bootstrap option is refused with the same words —
    and goes through the round-trip editor, so comments and ``!file``
    references elsewhere in the file survive the edit byte for byte.
    """
    opt = _declared_or_refuse(name, declared_options)
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
    if opt.leaf:
        # The area is a section in the file, and an existing one is
        # written into rather than replaced: the section may hold other
        # options, and their comments and `!file` references are as much
        # somebody's work as the rest of the file.
        section = data.get(opt.area)
        if section is None:
            section = {}
            data[opt.area] = section
        elif not isinstance(section, dict):
            raise ConfigError(
                f"The section {opt.area!r} in {file.name} is not a mapping of "
                "`option: value` pairs.",
                location=Location(file=file, key=opt.area),
                hint=f"it holds the options of {opt.area}, one per line:\n"
                f"    {opt.area}:\n      {opt.leaf}: <value>",
            )
        section[opt.leaf] = value
    else:
        data[name] = value
    _dump_config(file, data, yaml)
    return value


def unset_config_value(
    file: Path,
    name: str,
    *,
    declared_options: tuple[Option, ...] = OPTIONS,
) -> bool:
    """Remove *name* from *file*; False when there was nothing to remove.

    The name must be a declared option — ``unset`` with a typo saying
    "nothing to remove" would confirm a removal that never happened.
    """
    opt = _declared_or_refuse(name, declared_options)
    yaml = editing_yaml()
    data = _load_for_editing(file, yaml)
    if data is None:
        return False
    if opt.leaf:
        section = data.get(opt.area)
        if not isinstance(section, dict) or opt.leaf not in section:
            return False
        del section[opt.leaf]
        # An empty section is removed with its last option: a `build:`
        # with nothing under it configures nothing and reads as an
        # unfinished edit to the next person opening the file.
        if not section:
            del data[opt.area]
        _dump_config(file, data, yaml)
        return True
    if name not in data:
        return False
    del data[name]
    _dump_config(file, data, yaml)
    return True
