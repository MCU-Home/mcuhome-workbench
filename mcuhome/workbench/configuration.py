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

One **entry** of such a map is written and removed key by key
(``builder.attic.target``, ``registry.packages.mcuhome.org.mirrors.sdk``),
so a builder and a registry are configurable without hand-editing YAML.
:data:`MAP_ENTRY_OPTIONS` declares what an entry of each map carries,
:func:`option` answers that declaration for the key a person typed, and
:func:`set_config_value` parses the text through it. What one key cannot
say is whether the *entry* is complete — a remote builder needs its
server, and one ``config set`` writes one key — so an entry half-way
through being written is a state a file passes through, and the next
resolution is what says what is still missing.

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
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from mcuhome.model.errors import ConfigError, Location
from mcuhome.model.userpaths import config_dir, expand

from mcuhome.workbench import builders as builders_module
from mcuhome.workbench import packageregistry
from mcuhome.workbench.buildenvstore import (
    EXTRACTION_BOUNDS,
    KIND_SDK,
    KIND_TOOLS,
    KIND_WORKSPACE,
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
from mcuhome.workbench.diagnostics import Diagnostic
from mcuhome.workbench.loader import FileRef, editing_yaml, read_yaml_file
from mcuhome.workbench.project import BUILDER_SECRETS_DIR, Project, require_secret_file

__all__ = [
    "CONFIG_FILE",
    "CONFIG_ORIGINS",
    "CONFIG_SCOPES",
    "MAP_ENTRY_OPTIONS",
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
CONFIG_ORIGINS = (
    "default",
    "program",
    "system",
    "user",
    "project",
    "environment",
    "arguments",
)

#: Every kind an option may declare. The two map kinds parse and merge
#: themselves and are named after the area they hold; ``boolean`` is
#: carried by the entry of a map rather than by an option of the
#: registry, and is declared here because the entries are parsed by the
#: same machinery.
OPTION_KINDS = (
    "string",
    "path",
    "paths",
    "strings",
    "integer",
    "number",
    "boolean",
    "builder",
    "registry",
)

#: How a boolean is written wherever it is written as a word: in a
#: configuration file YAML answers with the value itself, and these are
#: the two spellings ``mcuhome config set`` takes for one.
_BOOLEANS = {"true": True, "false": False}


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
        default=EXTRACTION_BOUNDS[KIND_SDK],
        minimum=1,
        help="how much the SDK package may unpack to, in bytes",
    ),
    Option(
        "build.workspace_max_bytes",
        kind="integer",
        default=EXTRACTION_BOUNDS[KIND_WORKSPACE],
        minimum=1,
        help="how much the build workspace package may unpack to, in bytes",
    ),
    Option(
        "build.tools_max_bytes",
        kind="integer",
        default=EXTRACTION_BOUNDS[KIND_TOOLS],
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

#: What one **entry** of a map option carries, by the kind of map: the
#: key inside the entry, and the declaration its text is parsed through.
#: The names here are the shape a person reads (``builder.<name>.target``);
#: :func:`option` answers a copy named the way the caller asked, so a
#: client shows the key that was typed. No entry has an environment
#: variable or a flag, for the same reason the map itself has none: what
#: follows the area is data, and a grammar for it in a variable would be
#: a second one to specify and parse.
#:
#: What an entry key may *not* say is whether the entry is complete — a
#: remote builder needs its server, and one ``config set`` writes one
#: key. That is the resolution's to say, at the file, in the words that
#: name what is missing.
MAP_ENTRY_OPTIONS: dict[str, dict[str, Option]] = {
    "builder": {
        "target": Option(
            "builder.<name>.target",
            kind="string",
            choices=BUILD_TARGETS,
            environment=False,
            arguments=False,
            help="where a build at this builder runs",
        ),
        "server": Option(
            "builder.<name>.server",
            kind="string",
            environment=False,
            arguments=False,
            help="a remote builder's build server, as host[:port]",
        ),
        "container_image": Option(
            "builder.<name>.container_image",
            kind="string",
            environment=False,
            arguments=False,
            help="the build environment image a local builder builds in",
        ),
    },
    "registry": {
        "untrusted": Option(
            "registry.<base-domain>.untrusted",
            kind="boolean",
            default=False,
            environment=False,
            arguments=False,
            help="read this registry without checking any signature",
        ),
        "anchor": Option(
            "registry.<base-domain>.anchor",
            kind="path",
            environment=False,
            arguments=False,
            help="the trust anchor file this registry's signatures are held against",
        ),
        "mirrors": Option(
            "registry.<base-domain>.mirrors.<source>",
            kind="strings",
            environment=False,
            arguments=False,
            help="where one source of this registry is read from, in order",
        ),
    },
}

#: The one entry key that holds a map of its own, keyed by source name.
_MIRRORS_KEY = "mirrors"


@dataclass(frozen=True)
class MapEntry:
    """One key inside one entry of a map option, as a caller named it.

    *map_option* is the map the key lives in (``builder``, ``registry``),
    *name* the entry a person chose (the builder's name, the registry's
    base domain — which carries dots of its own, so it is read from the
    right), *keys* where the value sits below that entry, and
    *declaration* what the text is parsed through.
    """

    map_option: Option
    name: str
    keys: tuple[str, ...]
    declaration: Option


def find_map_entry(name: str, registry: tuple[Option, ...] = OPTIONS) -> MapEntry | None:
    """The map entry *name* names, or ``None`` where it names none.

    A ``find_``: a name that is not one of these is not an error here —
    it is an ordinary option key, or nothing, and whoever asked says so
    in their own words.

    Read from the **right**, because the entry's name is data and may
    hold dots: the last component is the key inside the entry, unless
    the one before it is ``mirrors``, which takes the source name with
    it. Everything left of that is the entry.
    """
    parts = name.split(".")
    if len(parts) < 3:  # noqa: PLR2004 - area, entry, key: the shortest entry key there is
        return None
    area, rest = parts[0], parts[1:]
    declared = next((one for one in registry if one.name == area and not one.leaf), None)
    if declared is None:
        return None
    entries = MAP_ENTRY_OPTIONS.get(declared.kind)
    if entries is None:  # pragma: no cover - a test holds the table to the map kinds
        return None
    if len(rest) >= 3 and rest[-2] == _MIRRORS_KEY and _MIRRORS_KEY in entries:  # noqa: PLR2004
        entry, keys = ".".join(rest[:-2]), (_MIRRORS_KEY, rest[-1])
    else:
        entry, keys = ".".join(rest[:-1]), (rest[-1],)
    template = entries.get(keys[0])
    if template is None or not entry:
        return None
    if keys == (_MIRRORS_KEY,):
        # `registry.<domain>.mirrors` names the map of sources, not a
        # value: which source is being pointed somewhere else is part of
        # the key, because a registry's sources are mirrored one by one.
        return None
    return MapEntry(
        map_option=declared,
        name=entry,
        keys=keys,
        declaration=replace(template, name=name),
    )


#: Keys a configuration file used to carry, and the option each of them
#: is today. A configuration file has no version and therefore no
#: migration — what a project's layout gets, a file in
#: ``$XDG_CONFIG_HOME`` or ``/etc`` cannot have — so the successor is
#: named where the old key is written instead, and the one edit a user
#: has to make is a line they can see.
RETIRED_OPTIONS: dict[str, str] = {
    "builders": "builder",
    "ccache_dir": "build.cache_root",
    "default_builder": "build.builder",
    "project_dir": "project.dir",
    "signing_key": "signing.key",
}

#: Environment variables MCUHome used to read, and what each is called
#: now. Unlike a retired key this is a **warning**, not a refusal: a
#: stale variable exported in somebody's shell profile would otherwise
#: block every command they run, including the one that would fix it.
RETIRED_VARIABLES: dict[str, str] = {
    "MCUHOME_CCACHE_DIR": "MCUHOME_BUILD_CACHE_ROOT",
    "MCUHOME_DEFAULT_BUILDER": "MCUHOME_BUILD_BUILDER",
    "MCUHOME_DOCKER": "MCUHOME_BUILD_CONTAINER_PROGRAM",
    "MCUHOME_IMGTOOL": "MCUHOME_SIGNING_IMGTOOL",
}

#: The directory a builder's credentials used to live in, beside the
#: configuration file of the user and system layers. Those two are
#: outside every project, so no upgrade reaches them.
RETIRED_BUILDER_SECRETS_DIR = "build-server"


def option(name: str, declared_options: tuple[Option, ...] = OPTIONS) -> Option:
    """The declaration of *name*, or a refusal naming what is declared.

    *name* is an option key (``build.mode``) or one entry key of a map
    option (``builder.attic.target``,
    ``registry.packages.mcuhome.org.mirrors.sdk``); an entry is answered
    with its own declaration, named the way it was asked for, so a client
    can show the kind and the help of the key a person typed.

    A name that is neither is refused in the words a configuration file
    is refused with — the same sentence and the same hint, because this
    is the lookup a person's typing reaches: a key that used to be an
    option names its successor, and everything else is answered with what
    a file may set.
    """
    for declared in declared_options:
        if declared.name == name:
            return declared
    entry = find_map_entry(name, declared_options)
    if entry is not None:
        return entry.declaration
    raise _refuse_undeclared(name, registry=declared_options)


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
    #: One of :data:`CONFIG_ORIGINS`: ``default``, ``program``, ``system``,
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
    if opt.kind == "boolean":
        if not isinstance(value, bool):
            raise refuse("true or false")
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
    if opt.kind == "boolean":  # pragma: no cover - no declared option carries one yet
        if value not in ("0", "1"):
            raise ConfigError(
                f"{opt.env_var} must be 1 or 0, not {value!r}.",
                hint=opt.help or None,
            )
        return value == "1"
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


def _refuse_retired_option(
    key: str,
    *,
    location: Location | None = None,
    registry: tuple[Option, ...] = OPTIONS,
) -> ConfigError:
    """The refusal for a key that was an option once, naming what it is now.

    A configuration file carries no version, so there is no migration
    that could rewrite it — the file in ``/etc`` belongs to the machine
    and the one in a user's configuration directory to them. What
    MCUHome can do is refuse at the line the old key stands on and say
    what to write instead, which is the one edit that makes the file
    current again.
    """
    successor = RETIRED_OPTIONS[key]
    opt = next((one for one in registry if one.name == successor), None)
    if opt is None:  # pragma: no cover - a test holds the table to the registry
        hint = f"it is {successor!r} now."
    elif opt.bootstrap or not opt.files:
        # The successor cannot be written into a file either, so the
        # hint is the channel refusal's — one source for the spellings a
        # per-invocation value is set with.
        hint = f"it is {successor!r} now, and {_refuse_not_file_settable(opt, location).hint}"
    elif successor == "builder":
        # The one retired key whose successor is a map, and the only
        # place the entries' own renames can be said.
        hint = (
            "it is the map 'builder' now, one section per builder, keyed by its name:\n"
            "    builder:\n"
            "      <name>:\n"
            "        target: remote\n"
            "        server: <host[:port]>\n"
            "Inside an entry, `type:` is `target:` and `image:` is `container_image:`."
        )
    elif not opt.leaf:  # pragma: no cover - no other map has a retired name
        hint = (
            f"it is the map {successor!r} now, one section per entry, keyed by its name:\n"
            f"    {successor}:\n      <name>:\n        <option>: <value>"
        )
    else:
        hint = f"it is {successor!r} now; write it as:\n    {opt.area}:\n      {opt.leaf}: <value>"
    return ConfigError(
        f"There is no option called {key!r}.",
        location=location,
        hint=hint,
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
        if key in RETIRED_OPTIONS:
            raise _refuse_retired_option(str(key), location=location, registry=registry)
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
    on_warning: Callable[[Diagnostic], None] | None = None,
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

    A configuration file holding a key that *used* to be an option is
    refused at the line it stands on, naming the option it is today
    (:data:`RETIRED_OPTIONS`); a retired **environment variable** draws a
    ``retired_environment_variable`` warning through *on_warning*
    instead. The asymmetry is deliberate: a file is edited once by
    whoever owns it, while a stale variable exported in a shell profile
    would refuse every command including the one that fixes it.
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

    if on_warning is not None:
        for retired, successor in RETIRED_VARIABLES.items():
            if env.get(retired):
                on_warning(_retired_variable_warning(retired, successor))

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


def _retired_variable_warning(retired: str, successor: str) -> Diagnostic:
    """The finding for a variable that is set and is not read any more.

    It carries no location: an environment variable stands in no file,
    and a finding about nothing in particular renders without a place
    rather than with a wrong one.
    """
    return Diagnostic.warning(
        f"{retired} is set, and MCUHome does not read it any more.",
        kind="retired_environment_variable",
        hint=(
            f"it is {successor} now. Export that one instead, and unset {retired} so "
            "it cannot mislead the next person who reads your environment."
        ),
    )


def _settable(name: str, registry: tuple[Option, ...], *, channel: str) -> Option:
    """The declaration of *name*, or a programming error naming the channel.

    The lookup is done here rather than through :func:`option`, which
    refuses in words: what arrives on these two channels was put there by
    a tool — the arguments channel carries what a parser derived from
    this very registry — so a name nobody declared is a defect in that
    tool and not something to word for a user.
    """
    opt = next((one for one in registry if one.name == name), None)
    if opt is None:
        raise ValueError(f"{name!r} is not a declared option")
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
    on_warning: Callable[[Diagnostic], None] | None = None,
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


def _refuse_retired_credentials(found: Path, expected: Path, name: str) -> ConfigError:
    """A credentials file under the name the layout used to give it.

    Refused rather than read: the file holds a token, and quietly
    ignoring it would make a build that should reach a builder go
    somewhere else — or nowhere — with nothing said. Refused rather than
    followed, too: one name for one thing is what the layout is for, and
    a second path that also works is a second layout.
    """
    return ConfigError(
        f'The credentials of the builder "{name}" are at {found}, which MCUHome does '
        "not read any more.",
        location=Location(file=found),
        hint=(
            f"a builder's credentials live under the builder's own directory now. "
            f"Move the file:\n    mv {found} {expected}\n"
            f"and remove {found.parent} once it is empty."
        ),
    )


def _builder_token(
    name: str,
    *,
    project: Project | None,
    env: Mapping[str, str],
    on_warning: Callable[[Diagnostic], None] | None,
) -> str | None:
    relative = Path(BUILDER_SECRETS_DIR) / f"{name}.yaml"
    retired = Path(RETIRED_BUILDER_SECRETS_DIR) / f"{name}.yaml"
    secrets_dirs: list[Path] = []
    if project is not None:
        secrets_dirs.append(project.secrets_dir)
    for directory in (user_config_dir(env), system_config_dir(env)):
        if directory is not None:
            secrets_dirs.append(directory / "secrets")
    for secrets_dir in secrets_dirs:
        file = secrets_dir / relative
        if not file.is_file():
            # The old name, at the rung that would otherwise be walked
            # past in silence. A project's layout is moved by its
            # upgrade; the user and system directories are outside every
            # project, so the file is named here instead.
            if (secrets_dir / retired).is_file():
                raise _refuse_retired_credentials(secrets_dir / retired, file, name)
            continue
        require_secret_file(file, key_material=False, on_warning=on_warning)
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
            require_secret_file(token.path, key_material=False, on_warning=on_warning)
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
    for declared in registry:
        if declared.name == name:
            return declared
    raise _refuse_undeclared(name, registry=registry)


def _refuse_undeclared(name: str, *, registry: tuple[Option, ...]) -> ConfigError:
    """The one refusal for a key nobody declares, wherever it was typed.

    A key that was an option once names its successor; a key inside one
    of the maps is answered with the entry keys that map takes, because
    "there is no option called builder.attic.typo" without them is a
    list a person cannot guess.
    """
    if name in RETIRED_OPTIONS:
        return _refuse_retired_option(name, registry=registry)
    area = name.partition(".")[0]
    declared = next((one for one in registry if one.name == area and not one.leaf), None)
    entries = MAP_ENTRY_OPTIONS.get(declared.kind) if declared is not None else None
    if entries is not None:
        shown = ", ".join(sorted(entry.name for entry in entries.values()))
        return ConfigError(
            f"There is no option called {name!r}.",
            hint=(
                f"{area!r} is a map, written one entry key at a time — the keys an "
                f"entry of it takes are: {shown}"
            ),
        )
    settable = sorted(one.name for one in registry if one.files and not one.bootstrap)
    return ConfigError(
        f"There is no option called {name!r}.",
        hint="options settable from a configuration file: " + ", ".join(settable),
    )


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
    if opt.kind in MAP_ENTRY_OPTIONS:
        shown = "\n    ".join(
            f"mcuhome config set {entry.name} <value>"
            for entry in MAP_ENTRY_OPTIONS[opt.kind].values()
        )
        raise ConfigError(
            f"{opt.name!r} is a map of entries and not settable as one value.",
            location=location,
            hint=f"set one entry key at a time:\n    {shown}",
        )
    if opt.kind == "boolean":
        if text not in _BOOLEANS:
            raise ConfigError(
                f"{opt.name} is either true or false, not {text!r}.",
                location=location,
                hint=opt.help or None,
            )
        return _BOOLEANS[text]
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

    *name* is an option key or one **entry key** of a map option
    (``builder.attic.target``, ``registry.packages.mcuhome.org.untrusted``,
    ``registry.packages.mcuhome.org.mirrors.sdk``). An entry key is
    written into the map's section, under the entry a person named,
    creating what is not there yet; the text is parsed through that
    entry's own declaration, so a value the key cannot take is refused
    here. Whether the **entry** is complete is not this call's to say —
    one ``config set`` writes one key, and a remote builder that has not
    got its server yet is a state the file passes through until the next
    one; the resolution is what names what is missing.
    """
    location = Location(file=file, key=name)
    entry = find_map_entry(name, declared_options)
    if entry is not None:
        return _set_map_entry(entry, file, text, env=env, location=location)
    opt = _declared_or_refuse(name, declared_options)
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

    The name must be a declared option or one entry key of a map option —
    ``unset`` with a typo saying "nothing to remove" would confirm a
    removal that never happened.

    An entry key takes what it empties with it: the last key of an entry
    removes the entry, and the last entry removes the map, because a
    ``builder:`` with nothing under it configures nothing and reads as an
    unfinished edit to whoever opens the file next.
    """
    entry = find_map_entry(name, declared_options)
    if entry is not None:
        return _unset_map_entry(entry, file)
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


def _set_map_entry(
    entry: MapEntry,
    file: Path,
    text: str,
    *,
    env: Mapping[str, str],
    location: Location,
) -> Any:
    """Write one key of one map entry, creating the way down to it."""
    value = _value_to_write(entry.declaration, text, location)
    # The same proof a plain option's value goes through, against the
    # entry key's own declaration: a `target` outside the vocabulary or
    # an `untrusted` that is not a boolean is refused before the file is
    # touched.
    _parse_file_value(
        entry.declaration, value, file=file, env=env, location=location, origin="edit"
    )
    yaml = editing_yaml()
    data = _load_for_editing(file, yaml)
    if data is None:
        data = {}
    holder: Any = _section_for(data, entry.map_option.name, entry, file)
    holder = _section_for(holder, entry.name, entry, file)
    for step in entry.keys[:-1]:
        holder = _section_for(holder, step, entry, file)
    holder[entry.keys[-1]] = value
    _dump_config(file, data, yaml)
    return value


def _section_for(holder: Any, key: str, entry: MapEntry, file: Path) -> Any:
    """The mapping under *key*, created where it is not there yet.

    An existing one is written into rather than replaced: it may hold
    other entries, other keys and their comments, and those are as much
    somebody's work as the rest of the file.
    """
    found = holder.get(key)
    if found is None:
        found = {}
        holder[key] = found
    elif not isinstance(found, dict):
        raise ConfigError(
            f"{key!r} in {file.name} is not a mapping, and {entry.declaration.name} "
            "is written inside one.",
            location=Location(file=file, key=key),
            hint=(
                f"an entry of {entry.map_option.name!r} is a section with its keys "
                f"below it:\n    {entry.map_option.name}:\n      {entry.name}:\n"
                f"        {entry.keys[-1]}: <value>"
            ),
        )
    return found


def _unset_map_entry(entry: MapEntry, file: Path) -> bool:
    """Remove one key of one map entry, and whatever it leaves empty."""
    yaml = editing_yaml()
    data = _load_for_editing(file, yaml)
    if data is None:
        return False
    # The way down, kept so that what an edit empties can be removed on
    # the way back up: the map, the entry, and for a mirror list the
    # `mirrors` section between them.
    path = [entry.map_option.name, entry.name, *entry.keys]
    holders: list[Any] = [data]
    for step in path[:-1]:
        found = holders[-1].get(step) if isinstance(holders[-1], dict) else None
        if not isinstance(found, dict):
            return False
        holders.append(found)
    if path[-1] not in holders[-1]:
        return False
    del holders[-1][path[-1]]
    for holder, step in zip(reversed(holders[:-1]), reversed(path[:-1]), strict=True):
        if holders[-1]:
            break
        del holder[step]
        holders.pop()
    _dump_config(file, data, yaml)
    return True
