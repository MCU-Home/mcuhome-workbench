# The workbench API

This document describes the programmatic surface of `mcuhome.workbench`
as of the workbench version that ships it.

`mcuhome.workbench.api` is that surface. Everything else under
`mcuhome.workbench` is an implementation detail: names there may move
between releases without notice, and a program that imports them is on
its own. Everything a consumer needs is exported from `api` — the
`mcuhome` command line and the build server import from it and from
nothing else.

```python
from mcuhome.workbench import api

project, entry = api.resolve_device(
    "thermostat", env=os.environ.copy(), cwd=Path.cwd()
)
model = api.load_model(entry, project=project)
```

## Contents
1. [Conventions](#conventions)
2. [Projects and devices](#projects-and-devices) (incl. [Secrets](#secrets))
3. [Device models](#device-models)
4. [Configuration](#configuration)
5. [Builders](#builders)
6. [Building firmware](#building-firmware)
7. [Build contexts](#build-contexts)
8. [Build environments](#build-environments)
9. [Packages and registries](#packages-and-registries)
10. [Signing, reports and OTA](#signing-reports-and-ota)
11. [Checking a build host](#checking-a-build-host)
12. [Upgrading a project](#upgrading-a-project)
13. [Errors](#errors)
14. [Constants](#constants)
15. [Options](#options)
16. [Environment variables](#environment-variables)
17. [Files and directories](#files-and-directories)
18. [Documents](#documents)
19. [Names re-exported from the device-model package](#names-re-exported-from-the-device-model-package)
20. [What is not public](#what-is-not-public)
21. [Index of exported names](#index-of-exported-names)

## Conventions
These hold for every name below; they are stated once here rather than
repeated per function.

**Function names.** A function whose name starts with a verb does work —
it touches a file, a process, a socket, or applies the project's
resolution rules. A function named after a noun only answers, from its
arguments or from the package's own tables. `is_*` functions answer a
bool.

The verbs and what each one means: `resolve_` applies the project's rules
(a ladder, the configuration, a default) and answers the one value they
imply, refusing in words when there is none; `find_` searches for
something that may exist and answers `None` or an empty tuple when it
does not — a `find_` never raises over "not there"; `require_` refuses
unless a stated condition holds and answers nothing; `check_` examines
and **reports** findings in a result, raising nothing over what it found;
`read_` reads one file into an object; `write_` writes one object into a
file; `load_` reads, parses, validates and resolves; `open_` takes a
handle on something with a lifecycle; `create_` brings something new into
existence and refuses to overwrite where it writes to disk; `generate_`
produces content from its inputs alone; `render_` produces text in memory
and writes nothing; `build_` runs a build; `sign_` signs what a build
produced; `provision_` makes a build environment ready in the store;
`fetch_` gets bytes from a source to a local path; `ensure_` makes sure
something is present locally; `lock_` closes something against further
change by computing its identity; `clean_` removes what a previous run
produced; `delete_` removes something the user created; `rename_` moves
it under a new name; `reveal_` answers a secret value that every other
function masks; `plan_` answers what would run; `set_`/`unset_` change
one value in a file this package owns; `parse_` turns one text into the
value it denotes.

**Class names.** `*Request` is what a caller hands to one operation;
`*Options` is a resolved configuration section and a property of the
machine; `*Result` is what a reporting operation answers; `New*` is what
a `create_*` answers; `*Session` is a stateful handle, always obtained
from an `open_*` function. Alternative constructors are `from_<source>`
classmethods.

**Parameters.** At most two positional parameters, and only for the
subject of the call; everything else is keyword-only. One name means one
thing across the whole surface: `project`, `settings`, `options`, `env`,
`cwd`, `out_dir`, `work_root`, `sdk_sources`, `signing_pub`,
`container_image`, `declared_options`.

**The caller's context.** This package never reads `os.environ` and never
guesses a working directory. The environment mapping and the working
directory enter the surface only where a channel is actually read: the
project and device bootstrap (`resolve_project`, `resolve_device`,
`find_project_root`), the configuration layer (`resolve_settings`), and
the operations that start a child process. Every other function takes
what was already resolved — a `Project`, a `Settings`, a `BuildOptions`.
Two further functions take `env` for one stated reason each and read no
option from it: the upgrade functions take a project root as a `Path`,
because a project that needs upgrading cannot be resolved into a
`Project`, and `resolve_cache_root` takes `env` because its fallback is a
host path.

**Callbacks.** `on_*` parameters are keyword-only, default to `None`
("nothing to report"), and their return value is ignored — they are never
control flow. The vocabulary is fixed and append-only: `on_line(str)` for
log output, `on_warning(Diagnostic)` for a located, non-fatal finding,
`on_step(key, **facts)` for progress, `on_wait(SeatWait)` for a build
waiting for a turn, `on_container(str)` for a container a call started,
so the caller can reap it. A seam that decides control flow is a
`should_*` predicate supplied by the caller and polled by the callee;
`should_stop` is the only one.

**Synchrony, stopping and concurrency.** A function is `async` exactly
when it awaits something itself. `build_firmware` is the only one.
Everything else is synchronous, including the backend seam
(`BuilderSession.invoke`, `Step.run`), which blocks on a container
deliberately: a build server owns its own concurrency, and an awaitable
there would impose one model on every server that embeds this package. A
caller with an event loop offloads the synchronous operations with
`asyncio.to_thread`.

Because a build runs in a worker thread, **cancelling the awaiting task
does not stop it**. Stopping is `BuildRequest.should_stop`, a predicate
polled on the same tick as the deadline: the build walks the ladder it
walks for a deadline — a signal after the grace period, a kill ten
seconds later, the container removed — releases the build lock, leaves
`out_dir` with whatever was written before the stop, and answers
`ok=False, stopped=True`. `UpgradeSession.apply` takes the same
predicate.

Value objects on this surface are frozen and safe to share between
threads: `Project`, `Settings`, `BuildOptions`, every `*Result`. Handles
are not: one `BuilderSession`, one `UpgradeSession` or one held build
lock belongs to the thread that opened it. The build lock guards a build
directory **between processes** and counts nesting within one process, so
two builds of the *same* directory in one process are not refused by it —
a program that builds concurrently gives each build its own directory,
which is what `<project>/build/<device>/` does by construction.

**Results.** Every result class, and every value a result can carry, has
`to_dict() -> dict[str, Any]` answering JSON-ready data: no `Path`, no
tuples, no dataclasses. A document always carries every key it declares;
an absent value is `null`, `[]` or `{}`, never a missing key. The verdict
of a result is `ok`, the first key of its document; a `status` string
exists beside it only where the vocabulary has more than two values
(`StepResult`), and a run that was stopped rather than failed says so in
`stopped`. A function that answers with a document another program wrote
(a build report) answers with a plain `dict`.

**Findings.** A non-fatal finding is a `Diagnostic`: `severity`,
`message`, `location`, `key`, `hint`, `kind` — the error document's own
keys plus the severity. Results that can carry findings answer one
`diagnostics` list holding errors and warnings together, so a client
renders one list and never merges two.

**Sequences** in an answer are tuples. `to_dict()` turns them into lists.

## Projects and devices
A project is a directory with a `.mcuhome-project-root` marker. Resolving
one is the bootstrap every other call depends on.

```python
def resolve_project(
    project_dir: Path | str | None = None,
    *,
    env: Mapping[str, str],
    cwd: Path,
    require_version: bool = True,
) -> Project
```
The bootstrap ladder: *project_dir* first, `MCUHOME_PROJECT_DIR` in *env*
second, the upward marker search from *cwd* last. With
*require_version* the project's layout version is enforced: an older
project raises `ProjectUpgradeRequired`, a newer one
`ProjectVersionUnsupported`, and one whose marker an upgrade has renamed
`UpgradeInProgress` or `UpgradeInterrupted`. `require_version=False` is
for the caller that exists to fix the first of them. Raises
`ProjectFileError` for a marker that cannot be read.

```python
def read_project(root: Path, *, require_version: bool = True) -> Project
```
The project at a known root, without the ladder: reads the marker into a
`Project`. Same refusals.

```python
def create_project(root: Path, *, force: bool = False) -> NewProject
```
Creates the durable part of a project: the marker, `mcuhome.yaml`,
`devices/`, `secrets/` (mode 0700), the bundled trust anchor and a
`.gitignore`. Raises `ProjectFileError` over an existing project unless
*force*, and `ConfigError` when the directory cannot be written.

```python
def find_project_root(start: Path) -> Path | None
def is_project_root(path: Path) -> bool
def is_upgrading(path: Path) -> bool
```

```python
def resolve_device(
    spec: str,
    *,
    env: Mapping[str, str],
    cwd: Path,
    project_dir: Path | str | None = None,
) -> tuple[Project, Path]
```
A device name or path, and the project it belongs to. A name is a folder
under the project's `devices/`, the project coming from the same ladder
`resolve_project` walks. A path is a device folder or a YAML file, and
the project is then the one that path lies in — or, for a file outside
any project, **its own directory standing in for one**
(`Project(discovered=False)`), which is what makes a `secrets/main.yaml`
next to the file work. Raises `ConfigError` naming the devices the
project has when a name matches nothing.

For a device whose project is already in hand, `Project.device_file(name)`
answers the path without touching the disk.

```python
def create_device(
    name: str,
    *,
    project: Project,
    board: str,
    friendly_name: str | None = None,
    outline: DeviceOutline | None = None,
) -> NewDevice

def render_device_file(
    name: str,
    *,
    board: str,
    friendly_name: str | None = None,
    outline: DeviceOutline | None = None,
) -> str
```
`render_device_file` is pure — it answers the text, so a caller can show
it before anything is written. `create_device` writes it under
`devices/<name>/main.yaml` and refuses rather than overwriting. Given a
`DeviceOutline` both write real sections instead of the commented
example. Raise `ConfigError` for a name or board the registry does not
accept.

```python
def create_pairing(
    entry: Path,
    *,
    project: Project,
    force: bool = False,
    draw: Callable[[], Pairing] = random_pairing,
) -> NewPairing
```
Draws a device's commissioning credentials once: `!secret` references
into `main.yaml`, the values into the device's own secrets file beside
the project's `secrets/main.yaml`. Raises `ConfigError` when the device
already has credentials unless *force*, and when the file cannot be
edited in place. *draw* is the source of randomness, injected so a test
can pin it. `read_pairing` answers what a device already has.

```python
def rename_device(name: str, *, project: Project, to: str) -> tuple[Path, ...]
def delete_device(
    name: str, *, project: Project, keep_secrets: bool = False
) -> tuple[Path, ...]
```
Both answer every path they changed, and both hold the device's build
directory for the duration (the `rename` and `delete` lock operations),
so a run in flight refuses in words — `BuildDirectoryBusy` — instead of
losing its output. `rename_device` moves `devices/<name>/` and
`secrets/device/<name>.yaml`, and **removes** `build/<name>/`: build
output names the device inside its own report, so a moved build
directory would describe a device that no longer exists.
`delete_device` removes the device folder and its build directory, and
the device's secrets file unless *keep_secrets* — commissioning
credentials a controller already knows cannot be drawn again. Both raise
`ConfigError` for a name the project does not have, and `rename_device`
for a target name that is taken.

```python
def read_pairing(entry: Path, *, project: Project) -> Pairing | None
```
The commissioning credentials a device already has, or `None`. The
counterpart of `create_pairing`, which refuses rather than replacing
them, and the only way to show a device's codes without drawing new ones.

```python
def require_secret_file(
    path: Path,
    *,
    key_material: bool,
    on_warning: Callable[[Diagnostic], None] | None = None,
) -> None
def read_yaml_file(path: Path) -> Any
```
`require_secret_file` refuses with `ConfigError` for a world-readable
file holding key material and reports other exposed secrets through
*on_warning*. `read_yaml_file` parses one YAML file, turning parser
errors into `ConfigError`.

### Secrets
A project keeps its secrets in `secrets/`, one file per kind and name
(`SECRET_KINDS`: `main`, `device`, `builder`, `signing`). These six
functions are the only supported way to look at them, and **no document
any of them answers carries a value**.

```python
def find_secret_scopes(project: Project) -> tuple[SecretScope, ...]
def read_secrets(project: Project, *, kind: str, name: str = "") -> SecretFile
def reveal_secret(project: Project, *, kind: str, name: str = "", key: str) -> str
def set_secret(
    project: Project, *, kind: str, name: str = "", key: str, value: str
) -> None
def unset_secret(project: Project, *, kind: str, name: str = "", key: str) -> bool
def delete_secret_file(project: Project, *, kind: str, name: str) -> bool
```
`find_secret_scopes` answers every scope the project could have and
whether its file exists — which devices and which builders have one.
`read_secrets` answers the keys of one file with **masked** values and,
for the shared file, which devices refer to each key.
`reveal_secret` is a verb of its own so that the one call which returns a
value cannot be made by accident; it answers exactly the key asked for.
`set_secret` writes one key, creating the file with mode 0600 if it is
the first; `unset_secret` removes one and answers whether it was there;
`delete_secret_file` removes a whole `device` or `builder` file and
refuses for `main` and `signing`, which are the project's own and are
emptied key by key rather than removed under a user's feet.

Every one of them calls `require_secret_file` first, so an exposed file
is refused rather than read. `ConfigError` names the file for a scope
that does not exist, a kind outside `SECRET_KINDS`, or a key a file does
not hold.

`SecretScope` (frozen): `kind`, `name` (empty for `main` and `signing`),
`file`, `exists`, `to_dict()`.
`SecretKey` (frozen): `key`, `masked`, `used_by`, `to_dict()`.
`SecretFile` (frozen): `scope`, `keys`, `to_dict()`.

### Project
Frozen dataclass. Fields `root`, `discovered: bool`, `file: ProjectFile |
None`. Properties `marker`, `id`, `config_file`, `devices_dir`,
`secrets_dir`, `secrets_file`, `signing_secrets_file`. Methods
`builder_secrets_file(name)`, `device_secrets_file(name)`,
`device_file(name)`, `device_names()`, `to_dict()` → `{root, id,
discovered, version}`. `discovered` is false for the stand-in project a
device file outside any project gets.

### ProjectFile
Frozen dataclass. Fields `root`, `version: int`, `id: str | None`,
`upgrade: UpgradeRecord | None`. Properties `current`, `short_id`,
`token`; method `matches(given)`.

### NewProject, NewDevice, NewPairing
What the three `create_*` functions answer, each with `to_dict()`:
- `NewProject(project, created: tuple[Path, ...])`
- `NewDevice(project, entry, name, board)`
- `NewPairing(entry, secrets_file, pairing: Pairing, replaced: bool)`

### DeviceOutline and its choices
Frozen dataclasses a form-driven client assembles and hands to
`create_device`: `DeviceOutline(buses, peripherals, endpoints)` with
`is_empty()`; `BusChoice(id, controller)`; `PeripheralChoice(id, driver,
bus, address)`; `EndpointChoice(device_type, clusters)`;
`ClusterChoice(cluster, source, sampling)`.

## Device models
```python
def load_model(
    entry: Path, *, project: Project, on_warning: Callable[[Diagnostic], None] | None = None
) -> DeviceModel
```
Loads, validates and resolves one device configuration into the canonical
model — the single representation between YAML and every generator, and
the wire format of a remote build. Raises `ConfigError` for a single
problem and `ConfigErrorGroup` when validation found several.
*on_warning* receives the non-fatal findings.

```python
def validate_device(
    entry: Path, *, project: Project, on_warning: Callable[[Diagnostic], None] | None = None
) -> ValidationResult
```
The same work, reporting every problem instead of raising: one pass, all
markers.

```python
def read_model(path: Path) -> DeviceModel
```
A canonical model back from JSON — the other end of the wire.

```python
def generate_application(model: DeviceModel, *, out_dir: Path) -> tuple[Path, ...]
```
Writes the standalone Zephyr application the model describes into
*out_dir* and answers every file written, in the order they were written.
A build does not take this path — a build environment generates from the
model its context carries — so this is the caller who wants the tree for
its own sake. Raises `CompilerUnavailable` where the generator package is
not installed, and `GenerationError` for a model it cannot render.

```python
def device_schema() -> dict[str, Any]
def device_registry() -> dict[str, Any]
def to_json(data: dict[str, Any]) -> str
```
`device_schema` is the JSON Schema of a device file (`main.yaml`);
`device_registry` is what MCUHome knows about hardware and Matter, as
data a picker can consume. Both are pure answers an editor caches.

```python
def expand_user_path(path: Path | str, *, env: Mapping[str, str]) -> Path
```
Resolves a leading `~` against the stated environment rather than the
process's.

### ValidationResult
Frozen dataclass. Fields `entry`, `project`, `model: DeviceModel | None`,
`errors: tuple[MCUHomeError, ...]`, `warnings: tuple[Diagnostic, ...]`.
Property `ok` (true exactly when `model` is not `None` — a warning does
not make a configuration invalid). Methods `error_dicts()`,
`diagnostics()` (errors and warnings as one list, in file order),
`raise_errors()` — the bridge back to the raising style — and
`to_dict()`.

## Configuration
Five layers, ascending, over one declared option registry; each value
carries the layer it came from:

```
default → system file → user file → project file → environment → arguments
```

```python
def resolve_settings(
    *,
    project: Project | None,
    env: Mapping[str, str],
    args: Sequence[Argument] = (),
    program: ProgramDefaults | None = None,
    declared_options: tuple[Option, ...] = OPTIONS,
) -> Settings
```
*project* may be `None` outside a project — the project layer is then
absent. *args* carries the invocation's values and **only what the caller
was actually given**: an unset flag is absent, not `None`, because "the
flag was not used" and "the flag was used to clear the value" are
different statements. Bootstrap options are skipped; a file that sets one
is refused with the reason. Raises `ConfigError` for a value that does
not fit its declaration, naming the file and line that supplied it.

```python
@dataclass(frozen=True)
class Argument:
    name: str        # the option key
    value: Any       # what the tool parsed
    flag: str = ""   # the spelling the tool used; empty takes Option.flag

@dataclass(frozen=True)
class ProgramDefaults:
    name: str                  # the program, as `config print` names it
    values: Mapping[str, Any]  # its own defaults, by option key
```
The spelling travels so a later refusal can name what the person typed.
*program* is what an embedding program defaults shared keys to: it sits
directly above the declared defaults and below every file, and the
values it sets carry the origin `program` and the program's name as
their source. Both raise `ValueError` for a key nobody declared, for the
bootstrap option, and — for `args` — for an option the command line may
not set.

```python
def option(name: str, declared_options: tuple[Option, ...] = OPTIONS) -> Option
def resolve_config_file(scope: str, *, project: Project | None, env: Mapping[str, str]) -> Path
def set_config_value(
    file: Path, name: str, text: str, *, env: Mapping[str, str],
    declared_options: tuple[Option, ...] = OPTIONS,
) -> Any
def unset_config_value(
    file: Path, name: str, *, declared_options: tuple[Option, ...] = OPTIONS
) -> bool
```
`resolve_config_file` answers the file a scope (`system`, `user`,
`project`) is edited in. `set_config_value` parses *text* through the
option's declaration, writes it, and answers the parsed value;
`unset_config_value` answers whether anything was removed. Both raise
`ValueError` for a name nobody declared and `ConfigError` for a value the
declaration rejects.

### Option
Frozen dataclass — the single source of every spelling of one option.
Fields `name`, `kind`, `default`, `files`, `environment`, `arguments`,
`bootstrap`, `help`, `choices`, `minimum`.
Properties: `area` and `leaf` (the two halves of the key), `env_var`
(`MCUHOME_` plus the key uppercased, `.` and `_` as `_`; empty exactly
when `environment` is false), `flag` (`--` plus the key with `.` and `_`
as `-`; empty exactly when `arguments` is false).

### Setting, Settings
`Setting` is one resolved value: `option`, `value`, `origin` (one of
`CONFIG_ORIGINS`), `source` (the file for a file layer, the variable name
for the environment, the flag for an argument, `None` for a default), and
`to_dict()`.
`Settings` is the whole resolution: `setting(name)`, `value(name)`,
`origin(name)`, `__contains__`, and `to_dict()` answering `{<key>:
{value, origin, source}}` for every declared option, in declaration
order.

## Builders
A builder is a named place a build may run at, configured under the
`builder` area.

```python
def resolve_builder(
    settings: Settings,
    *,
    name: str | None = None,
    project: Project | None,
    env: Mapping[str, str],
    on_warning: Callable[[str], None] | None = None,
) -> SelectedBuilder
```
Which builder this invocation uses: an explicit *name*, the configured
`build.builder`, or the built-in `local` fallback — credentials from
`secrets/builder/<name>.yaml` included, looked up in the project, then
the user configuration directory, then the system one. Raises
`ConfigError` for a name nobody configured.

`Builder` (frozen): `name`, `target` (`local` or `remote`), `origin` (the
configuration layer that defined this entry, in the words `Setting.origin`
uses), `source` (the file it was defined in), `server`,
`container_image`, `to_dict()` — which carries both provenance keys,
because `mcuhome config print` owes the user both answers.
`SelectedBuilder` (frozen): `target`, `builder: Builder | None`,
`server`, `token`, `container_image`, `to_dict()` — which never carries
the token.

## Building firmware
```python
async def build_firmware(
    request: BuildRequest, *, target: BuildTarget | str | None = None
) -> BuildResult
```
One build, whichever target runs it. *target* takes a target object, a
target name (`local`, `remote`) which goes through
`resolve_build_target`, or `None` for "no preference", which takes the
request's builder and then `build.target`. The build directory is held
for the duration, so a second build of it refuses in words instead of
deleting this one's work.

A build that ran and **failed** is not an exception: it answers with
`BuildResult.ok` false, and a build that `should_stop` ended answers
`ok=False, stopped=True`. Exceptions are the refusals before or around
the work: `BuildDirectoryBusy`, `SdkUnavailable`, `EnvironmentUnavailable`,
`EnvironmentUnusable`, `BuildEnvironmentError`, `ContextFormatVersionError`,
`CompilerUnavailable`, `PackageRegistryError`, `TrustAnchorMissing`,
`ImageRegistryError`, `UnknownBuildTarget`, `UnknownBuildMode`,
`RemoteNotConfigured` and the `RemoteError` family. A target object this
package does not implement is a `TypeError`.

```python
def resolve_build_target(name: str | None) -> str
def resolve_build_mode(name: str | None) -> str
```
`None` and the empty string mean "no preference" and answer the default,
so a caller can hand through whatever its own ladder produced. Otherwise
`UnknownBuildTarget` / `UnknownBuildMode`, listing the real ones.

```python
@contextmanager
def open_build_lock(
    out_dir: Path, *, device: str = "", operation: str = "build"
) -> Iterator[None]
```
One build directory, one operation at a time (`LOCK_OPERATIONS`).
`build_firmware` takes it itself; a caller that does more to the same
directory — signing, flashing, deleting — holds it around the whole
sequence, and the nested acquisition inside the build then costs nothing.
Raises `BuildDirectoryBusy`, which names the holder.

```python
def is_busy(out_dir: Path) -> bool
def read_build(out_dir: Path) -> BuildRecord | None
def clean_build(out_dir: Path, *, device: str = "") -> tuple[Path, ...]
```
`is_busy` answers whether someone is working in *out_dir* right now,
without taking it — the question a caller asks when it wants to wait
rather than refuse. It writes nothing, so it never overwrites the holder
record that makes somebody else's refusal readable.

`read_build` answers what a build directory holds, for a client that
comes back to it later — after a restart, or in a second process — and
`None` for a directory that holds no build.
`BuildRecord(out_dir, device, context_id, artifacts, report,
signed: tuple[SignedArtifact, ...], container_image, busy)` with
`to_dict()`. It states what is there and re-verifies nothing: the hashes
in `artifacts` are the ones the build declared.

`clean_build` removes what a build produced, holding the directory under
the `clean` operation, and answers what it removed. It leaves the
directory itself and raises `BuildDirectoryBusy` when something is
running in it.

### BuildRequest
Frozen dataclass — everything a build may be given, whichever target runs
it. `model` and `out_dir` are the two every build needs; a field a target
does not use is ignored rather than refused.

| Field | Default | What it is |
|---|---|---|
| `model: DeviceModel` | — | what to build |
| `out_dir: Path` | — | where the unsigned artifacts and the build report end up |
| `env: Mapping[str, str]` | `{}` | the host facts to resolve tools and caches from |
| `options: BuildOptions \| None` | `None` | this machine's `build` section; `None` resolves it from `env` and `project_root` |
| `builder: SelectedBuilder \| None` | `None` | the selected destination: target, server, token, container image |
| `mode: str \| None` | `None` | this build's override of `build.mode` |
| `container_image: str \| None` | `None` | the build environment this build asks for, in the four pin forms; naming one for a build that starts no container is refused rather than half-honoured |
| `project_root: Path \| None` | `None` | where the trust anchors are; `None` builds from the configured sources alone |
| `registries: Sequence[RegistrySettings]` | `()` | mirror overrides and trust per base domain |
| `signing_pub: str` | `""` | PEM of the public signing key; becomes `keys/signing.pub` in the context |
| `patches_dir: Path \| None` | `None` | patches to carry into the context, laid out as `<layer>/NNNN-name.patch` |
| `context_dir: Path \| None` | `None` | a base context to build instead of creating one |
| `work_root: Path \| None` | `None` | scratch area; defaults to a hidden directory under `out_dir` |
| `wait_for_turn: bool` | `True` | wait when a build server has no room |
| `max_wait_seconds: float` | `DEFAULT_MAX_WAIT_SECONDS` | bound of that wait; `0` removes it |
| `on_line`, `on_step`, `on_wait` | `None` | the three progress callbacks |
| `should_stop: Callable[[], bool] \| None` | `None` | polled while the build runs; see [Synchrony, stopping and concurrency](#conventions) |

### BuildOptions
Frozen dataclass — the `build` section resolved once, and a property of
*this machine*: `target`, `mode`, `builder`, `container_repositories`,
`container_program`, `cpus`, `memory`, `env_store`, `dev_workspace`,
`python`, `sdk_sources`, `workspace_sources`, `tools_sources`,
`sdk_max_bytes`, `workspace_max_bytes`, `tools_max_bytes`, `cache_root`,
`cache_local`, `cache_shared`, `cache_session`, `cache_project`, and
`sources: Mapping[str, str]` — where each value came from. Methods
`source(leaf)`, `limits() -> BuildLimits`, `bound(kind)`, `to_dict()`.
Unset values are `None` and mean *the default of whatever consumes them*,
never a value invented here.

```python
def resolve_build_options(settings: Settings) -> BuildOptions
```

### Build targets and executions
A build has two placement questions and only the first belongs to a
caller: **where** it runs, and **how** the machine that runs it executes
the work.

```python
class BuildTarget: ...                      # base, no fields
class LocalBuild(BuildTarget):
    execution: Execution = ContainerExecution()
class RemoteBuild(BuildTarget):
    server: str | None = None
    token: str | None = None
    wait: bool = True
    max_wait_seconds: float = DEFAULT_MAX_WAIT_SECONDS
    container_image: str | None = None

class Execution: ...                        # base, no fields
class ContainerExecution(Execution):
    container_image: str | None = None
    cache_root: Path | None = None
class SubprocessExecution(Execution):
    cache_root: Path | None = None
    dev_workspace: Path | None = None
    stated_container_image: str | None = None
```
`RemoteBuild` carries no execution: a client can no more ask a build
server to run without a container than it can ask it to run with one.

### BuildResult
Frozen dataclass. Fields `ok`, `stopped`, `target`, `device`,
`context_id`, `artifacts: tuple[Artifact, ...]`, `out_dir`, `report` (the
report's file name in `out_dir`), `container_image` (the image that ran,
empty where none did), `detail`. Method `to_dict()` — see
[Documents](#documents). There is no `status`: a firmware build either
produced the artifacts or it did not, and a build environment that
answers `unsupported` to the build action is an unusable environment
(`EnvironmentUnusable`), not a failed build. `detail` is the
composition's own object, useful for logging and never part of a
document.

### Progress
`on_step(key, **facts)` is called when a build enters a step, and a
second time with facts once the step knows something worth stating.
Keys and fact names are append-only; a consumer renders what it
recognizes and ignores the rest.

| Key | Facts |
|---|---|
| `context` | `id`, `sdk`, `sdk_sha256`, `build_environment`, `board`, `files`, `patches`, `build_workspace`, `build_tools` |
| `environment` | `build_environment`, `zephyr`, `found_under`, `fetched` (container); `build_environment`, `fetched` (subprocess); none (remote) |
| `compile` | `container_image`, `cpus`, `memory_bytes` (local); `server` (remote) |

```python
def build_steps(
    *, target: BuildTarget | str | None = None, options: BuildOptions
) -> tuple[str, ...]
```
`BUILD_STEPS` is the whole ordered vocabulary above; `build_steps`
answers the ones *this* target will report, in order, resolved from the
same dispatch the build uses. That is what makes "step 2 of 3" true: the
build itself reports no count and no percentage, because a step knows
that it started and, later, what it found — and nothing in between.

`on_wait(SeatWait)` is called each time a build server refuses a turn.
`SeatWait` (frozen): `retry_after`, `waited`, `attempt`. It is
deliberately not a step: the build has not started and may never start.

## Build contexts
A build context is what a build is attributed to: the resolved pins, the
canonical model, the public signing key, the patches.

```python
def create_context(
    model: DeviceModel,
    *,
    out_dir: Path,
    work_root: Path,
    options: BuildOptions,
    signing_pub: str,
    patches_dir: Path | None = None,
    project_root: Path | None = None,
    registries: Sequence[RegistrySettings] = (),
    on_line: Callable[[str], None] | None = None,
) -> ContextRequest
```
Resolves every pin and writes a fresh base context at *out_dir*, which is
removed if it exists. The SDK constraint resolves to one release, that
release states the build workspace range it belongs with, and that
workspace states the tools range. A device that pins either overrides
that package alone, with a note on *on_line* rather than a refusal.
Raises `SdkUnavailable`, `PackageRegistryError`, `TrustAnchorMissing`,
`ConfigError`.

```python
def lock_context(out_dir: Path) -> ContextManifest
def verify_context(root: Path) -> ContextVerification
def read_context_manifest(path: Path) -> ContextManifest
def read_generator_chain(path: Path) -> tuple[GeneratorEntry, ...]
def read_context_facts(root: Path) -> dict[str, Any]
```
Locking computes the integrity list and the context ID — it is the act of
whoever builds the context, and a client that sent one checks the
identity the server answers with. `verify_context` answers a
`ContextVerification(root, manifest, actual_id, mismatches)` — `ok`
property, `to_dict()`, and `FileMismatch(path, declared_sha256,
actual_sha256)` entries with a `describe()` — rather than raising.
`lock_context` raises `ConfigError` for a context directory it cannot
close and `ContextFormatVersionError` for one this version does not read;
`read_context_manifest` raises the same for a manifest it cannot read.
`read_generator_chain` raises `ContextFormatVersionError` for a context
format this version does not read.

## Build environments
A build environment is the compiler stack a build runs in: a container
image, or two packages unpacked into a per-user store.

```python
def provision_environment(
    package: Path | str,
    *,
    options: BuildOptions,
    env: Mapping[str, str],
    sources: Sequence[Path] = (),
    on_line: Callable[[str], None] | None = None,
) -> StoreEntry
```
Acquires the package (operator directories first, the registry second,
the hash checked on every path), unpacks it under the bound for its kind,
finalizes and freezes it, and writes the marker last. A package that is
already in the store is answered without touching the network, the disk
or the lock. *package* is a package file — identified by the hash
computed from it — or a package name resolved against *sources* and the
registry. Raises `BuildEnvironmentError`, `SdkUnavailable`,
`PackageRegistryError`.

`StoreEntry` (frozen): `kind`, `name`, `version`, `sha256`, `path`,
`to_dict()`. `provision_environment` takes `should_stop` nowhere: it is
bounded by the package bound rather than by a caller's patience, and an
interrupted run leaves nothing a build can find (the marker is written
last).

```python
def open_builder_session(
    *,
    root: Path,
    context_dir: Path,
    sdk_tree: Path,
    launcher: Launcher,
    entry_point: Path | None = None,
    context_id: str = "",
    session_id: str | None = None,
    tiers: Mapping[str, CacheTier] | None = None,
    limits: BuildLimits | None = None,
    deadline_seconds: int = 5400,
    cancel_grace_seconds: int = 0,
) -> BuilderSession
```
The backend role, for the caller that owns its own sessions rather than
asking for a firmware: a build server. It is handed a context somebody
else created and locked, plus the environment that context pins, and
drives one step of the build-environment specification at a time. What a
local build does and what a build server does differ in who owns the
session, not in what a build is.

`BuilderSession` — attributes `root`, `context_dir`, `sdk_tree`,
`entry_point`, `launcher`, `context_id`, `session_id`, `tiers`, `limits`,
`deadline_seconds`, `cancel_grace_seconds`, `out_dir`, `home_dir`.
Methods `liveness(step)`, `prepare(action, *, parameters=None) -> Step`,
`invoke(action, *, parameters=None, on_line=None) -> StepResult`,
`close()`; it is a context manager. Steps run strictly one after another.
The split into `prepare` and `run` is what makes a step cancellable: the
sentinel whose existence means stop is known before the call that blocks.

`Step` — `session`, `invocation_id`, `action`, `base_dir`, `request`,
`work`, `out_dir`, `entry_point`, `cancel`, `cache`, `writable_cache`;
`result`, `stop()`, `run(*, on_line=None)`.

`StepResult` (frozen) — `action`, `context_id`, `exit_code`, `result`,
`status` (one of `STEP_STATUSES`), `problems`, `violation`, `artifacts`,
`out_dir`; property `ok`; `to_dict()`.

`CacheTier` (frozen) — `path`, `writable`.

`Liveness` (frozen) — what `BuilderSession.liveness(step)` answers: the
supervision policy of one step, `cancel` (the sentinel whose existence
means stop), `deadline_seconds` and `cancel_grace_seconds`.

```python
def resolve_cache_tiers(
    *,
    cache_root: Path | None = None,
    local: Path | None = None,
    shared: Path | None = None,
    session: Path | None = None,
    project: Path | None = None,
) -> dict[str, CacheTier]
def resolve_cache_root(*, options: BuildOptions, env: Mapping[str, str]) -> Path
def resolve_host_limits(*, cpus: float | None = None, memory_bytes: int | None = None) -> BuildLimits
def parse_memory(text: str | int | None, *, key: str = "build.memory") -> int | None
def resolve_shutdown_seconds(*, cancel_grace_seconds: float) -> float
def current_user() -> str | None
```
`parse_memory` accepts a byte count or a `k`/`m`/`g` suffix and raises
`ConfigError` naming *key* for anything else. `resolve_shutdown_seconds`
answers the whole liveness ladder from the cancel sentinel to the last
rung — the caller's grace period plus the fixed rungs. It is a bound, not
a promise. `current_user` answers `uid:gid` on POSIX and `None`
elsewhere.

### The container profile
```python
def resolve_container_program(*, options: BuildOptions) -> str
def require_container_runtime(runtime: ContainerRuntime, *, env: Mapping[str, str]) -> None
def require_container_image(
    declaration: Declaration, *, container_image: str, generator: str = "",
    zephyr_constraint: str = "",
) -> None
def ensure_container_image(
    runtime: ContainerRuntime, container_image: str,
    *, on_line: Callable[[str], None] | None = None,
) -> bool
def resolve_container_image(
    packages: Mapping[str, PackageMember],
    *,
    registry: ImageRegistry | None = None,
    repositories: Sequence[str] = DEFAULT_CONTAINER_REPOSITORIES,
    pin: ContainerImagePin | None = None,
    platform: str | None = None,
) -> ContainerImageMatch
def parse_container_image(text: str | None) -> ContainerImagePin
def create_launcher(
    *,
    container_image: str,
    runtime: ContainerRuntime,
    user: str | None = None,
    limits: ContainerLimits | None = None,
    on_container: Callable[[str], None] | None = None,
) -> Launcher
```
An image is chosen by the package set its labels declare, never by its
name: `resolve_container_image` answers a `ContainerImageMatch(reference,
declaration, found_under)` or raises `EnvironmentUnavailable`, and
`ImageRegistryUnreachable` where a registry could not be asked.
`require_container_image` refuses an image whose declaration does not
match this generation, generator constraint or Zephyr release
(`EnvironmentUnusable`). `require_container_runtime` refuses with the one
thing that is wrong — no program, or no daemon — and takes `env` because
that is what it probes. `ensure_container_image` pulls what is not
present and answers whether it had to, raising `ImageRegistryError` and
its two subclasses. `create_launcher` answers the `Launcher` a builder
session starts each step with and raises nothing; *on_container* receives
every container name it starts, so a caller can reap them.

`ContainerRuntime(program=DEFAULT_CONTAINER_PROGRAM, *, runner=None,
spawner=None)` — the seam over the container command line; methods `run`,
`spawn`, `present`, `pull`, `remove`.
`ContainerLimits(memory=None, cpus=None, pids=None)` with
`from_build_limits(limits, *, pids=DEFAULT_CONTAINER_PIDS)` and
`to_arguments()`.
`ImageRegistry` is the container-registry client
`resolve_container_image` may be handed; `ContainerImagePin(repository,
tag, digest)` with `stated()`, `canonical()`, `described()` is what
`parse_container_image` answers.
`Launcher` is `Callable[[Step, LineSink | None], Running]` — a type
alias, so a caller may supply its own.

## Packages and registries
```python
def open_package_registry(
    base_domain: str,
    *,
    project_root: Path,
    settings: Sequence[RegistrySettings] = (),
    into: Path,
    on_warning: Callable[[str], None] | None = None,
) -> RegistrySource
def fetch_sdk_package(
    *, version: str, sha256: str, sources: Sequence[Path], into: Path,
    registry: RegistrySource | None = None, max_bytes: int | None = None,
) -> AcquiredPackage
def resolve_package(
    pin: PackagePin, *, kind: str, sources: Sequence[Path] = (),
    registry: RegistrySource | None = None, platform: str | None = None,
) -> ResolvedPackage
def sha256_file(path: Path) -> str
```
`open_package_registry` defers everything to the first question actually
asked, so a build whose packages are already in the operator's
directories neither needs a trust anchor nor is stopped by a missing one;
the refusals (`PackageRegistryError`, `TrustAnchorMissing`) therefore
arrive at the point of use, not here. `fetch_sdk_package` raises
`SdkUnavailable` when no source holds the pinned package and
`PackageRegistryError` when a registry answered something unusable; it
answers an `AcquiredPackage(version, source, tree, name)`.
`resolve_package` raises `SdkUnavailable` for a constraint no published
version satisfies and answers a `ResolvedPackage(name, version, file,
sha256, size)`. `RegistrySource` is a type alias — a registry client or a
callable that builds one on first use.

`RegistrySettings` (frozen): `base_domain`, `untrusted`, `mirrors`,
`anchor`, `to_dict()`.

## Signing, reports and OTA
The private key never reaches a build. A build produces unsigned
artifacts and a build report; signing happens where the key is.

```python
def resolve_signing_key(
    override: Path | str | None = None,
    *,
    env: Mapping[str, str],
    project: Project | None = None,
) -> SigningKey
def create_signing_key(
    *,
    env: Mapping[str, str],
    project: Project | None = None,
    path: Path | None = None,
) -> SigningKey
def generate_key_pem() -> str
def public_key_pem(private_pem: str) -> str
def is_p256_private_key(text: str) -> bool
def is_p256_public_key(text: str) -> bool
```
Resolution order: *override*, then `signing.key`, then the project's
`secrets/signing/key.yaml` reference. `resolve_signing_key` **never
writes**: it raises `BuildError` when there is no key, when the file
cannot be read, when it is not a P-256 key, and `ConfigError` when the
file is exposed to other users. A caller that wants one generated says so
— `create_signing_key` writes the pair (mode 0600) and answers it with
`created` true, which is worth saying out loud: a device only accepts
images signed with the key its bootloader carries. `SigningKey` (frozen):
`path`, `pem`, `in_secrets`, `created`.

```python
def read_build_report(path: Path) -> dict[str, Any]
def plan_signing(
    out_dir: Path, *, env: Mapping[str, str], key: Path | str | None = None,
    project: Project | None = None,
) -> SignPlan
def sign_firmware(
    out_dir: Path, *, env: Mapping[str, str], key: Path | str | None = None,
    project: Project | None = None,
) -> SigningResult
```
`read_build_report` accepts a build directory or the report file inside
one and raises `BuildError` for a missing, unreadable or unknown report
version. `plan_signing` answers every command signing will run, decided
before any of them, so a caller can show them, and raises everything the
run itself would raise — a missing signing program, an unreadable key,
an artifact the report names and the directory does not hold — so that
`sign_firmware`'s own failure mode is "the signing program said no".
`SignPlan` (frozen): `out_dir`, `report_path`, `key`, `parameters`,
`commands`, `outputs`, `to_dict()`. `SigningResult` (frozen): `ok`,
`out_dir`, `report_path`, `key`, `signed: tuple[SignedArtifact, ...]`,
`to_dict()`.

```python
def write_ota_image(model: DeviceModel, *, payload: Path, out_dir: Path) -> OtaImage | None
def ota_file_name(device: str, version: str) -> str
def ota_parameters(model: DeviceModel) -> OtaIdentity | None
```
`write_ota_image` wraps a **signed** payload in the Matter OTA header and
answers `None` for a device that has no OTA identity. It raises
`BuildError` when the payload is missing or unreadable and `ConfigError`
when the device's stated version is not one the header can carry.

## Checking a build host
```python
def check_build_host(
    *, options: BuildOptions, env: Mapping[str, str], project: Project | None = None
) -> HostCheckResult
```
What a build on this machine would need, reported rather than raised.
Which checks run follows `options.mode`: the container runtime and the
image search only for `container`, the environment store and the
interpreter only for `subprocess`. `HostCheckResult(ok, findings)` with
`HostFinding(check, ok, detail, hint)`; `check` is one of
`container_runtime`, `container_image`, `env_store`, `python`,
`signing_imgtool`, `dev_workspace`, `cache_root` — each named after the
option or the thing it examined. Both have `to_dict()`. It raises
nothing: a host that cannot build is the answer, not an exception.

## Upgrading a project
```python
@contextmanager
def open_upgrade_session(root: Path) -> Iterator[UpgradeSession]
def plan_upgrade(from_version: int) -> tuple[Migration, ...]
def find_running_builds(root: Path) -> tuple[RunningBuild, ...]
```
The session renames the project marker for the whole run, so nothing else
can start work on a project being rewritten. A caller drives the three
apart on purpose: take the project, wait for what is still running, *then*
ask the user, then apply.

`UpgradeSession` — `root`, `file`, `path`, `plan`, `from_version`,
`failed`; `running_builds()`, `apply(*, on_step=None, should_stop=None)
-> UpgradeResult`. `on_step` receives the keys `migration_started` and
`migration_done` with the facts `name`, `from_version`, `to_version`;
`should_stop` is polled between migrations, and a stopped run answers
`UpgradeResult.stopped` with the migrations it did not reach in
`remaining`. Raises `MigrationFailed`, `UpgradeInProgress`,
`UpgradeInterrupted`.

`UpgradeResult` (frozen): `from_version`, `to_version`, `applied`,
`stopped`, `remaining`, `to_dict()`.
`Migration` (frozen): `from_version`, `to_version`, `name`,
`description`, `details`, `run`, `to_dict()`.
`RunningBuild` (frozen): `directory`, `device`, `operation`, `process`,
`started`, `name`, `to_dict()`.

## Errors
Every error this package raises derives from `MCUHomeError` and carries a
message written for the person who hit it, a location where there is one,
and a hint that names the fix. `to_dict()` answers the one error
document:

```json
{
  "message": "…", "file": "devices/thermostat/main.yaml", "line": 12,
  "column": 3, "key": "endpoints.0.clusters", "hint": "…",
  "kind": "ConfigError"
}
```

`kind` is the exception's class name, and the class names below are
therefore a stable, user-facing vocabulary. A client renders errors from
`error_dicts()` or `to_dict()` and never formats an exception itself.

```python
def error_dicts(exc: MCUHomeError, *, root: Path | None = None) -> list[dict[str, Any]]
```
Answers one dictionary per problem — several for a `ConfigErrorGroup` —
with file paths relative to *root* where one is given.

| Exception | Base | Raised when |
|---|---|---|
| `MCUHomeError` | `Exception` | the base of everything below |
| `ConfigError` | `MCUHomeError` | one problem in a configuration file; carries `location: Location` and `hint`, and has `render()` |
| `ConfigErrorGroup` | `MCUHomeError` | validation found several; carries `errors: list[ConfigError]` |
| `GenerationError` | `MCUHomeError` | the generator cannot render a model |
| `BuildError` | `MCUHomeError` | the base of every build refusal |
| `CompilerUnavailable` | `GenerationError` | the generator package is not installed |
| `BuildDirectoryBusy` | `BuildError` | another process holds the build directory; names the holder |
| `SdkUnavailable` | `BuildError` | the pinned SDK package is in no source; carries `version`, `sha256`, `searched` |
| `EnvironmentUnavailable` | `BuildError` | no build environment delivers the package set the context pinned |
| `EnvironmentUnusable` | `BuildError` | one was found but does not match this generation, generator or Zephyr release |
| `BuildEnvironmentError` | `BuildError` | provisioning a build environment failed |
| `ContextFormatVersionError` | `BuildError` | the context format is newer than this version reads; carries `found` |
| `PackageRegistryError` | `BuildError` | a package registry could not be read or verified |
| `TrustAnchorMissing` | `PackageRegistryError` | no trust anchor for a base domain |
| `ImageRegistryError` | `BuildError` | a container registry could not be reached or read |
| `ImageRegistryUnauthorized` | `ImageRegistryError` | it refused the credentials |
| `ImageRegistryUnreachable` | `ImageRegistryError` | it could not be reached |
| `UnknownBuildTarget` | `BuildError` | a target name that is not one of `BUILD_TARGETS` |
| `UnknownBuildMode` | `BuildError` | a mode name that is not one of `BUILD_MODES` |
| `RemoteNotConfigured` | `BuildError` | `remote` was selected and the server or the SDK pin is missing |
| `RemoteError` | `BuildError` | the base of every refusal from a build server |
| `RemoteDependencyMissing` | `RemoteError` | the client dependency for remote builds is not installed |
| `RemoteTransportError` | `RemoteError` | the connection failed |
| `ServerRefusal` | `RemoteError` | the server said no; carries `code`, `layer`, `retryable`, `details`, `verb`, `server_message` |
| `WaitedTooLong` | `RemoteError` | the wait bound ran out before a turn came free; carries `waited`, `attempts` |
| `ContextIdMismatch` | `RemoteError` | the server locked a context to a different identity |
| `ContextTooLarge` | `RemoteError` | the context exceeds what the server accepts |
| `PrivateKeyRefused` | `RemoteError` | something that is not a public key was about to travel |
| `ProjectFileError` | `MCUHomeError` | the project marker cannot be read |
| `ProjectUpgradeRequired` | `ProjectFileError` | the project's layout is older than `PROJECT_VERSION` |
| `ProjectVersionUnsupported` | `ProjectFileError` | it is newer |
| `UpgradeInProgress` | `ProjectFileError` | an upgrade is running |
| `UpgradeInterrupted` | `ProjectFileError` | one was interrupted |
| `MigrationFailed` | `MCUHomeError` | a migration refused or failed |

`Location` (frozen, re-exported): `file`, `line`, `column`, `key`.

## Constants
| Name | Value / meaning |
|---|---|
| `VERSION` | this package's version |
| `MODEL_VERSION` | the canonical model's format version |
| `MODEL_PACKAGE_VERSION` | the device-model package's release version |
| `PROJECT_VERSION` | the project layout version this package writes and requires |
| `SPEC_GENERATION` | the build-environment specification generation this package speaks |
| `PROJECT_MARKER_FILE` | `.mcuhome-project-root` |
| `UPGRADE_MARKER_FILE` | `.mcuhome-project-root.upgrade` |
| `PROJECT_CONFIG_FILE` | `mcuhome.yaml` |
| `CONFIG_FILE` | `configuration.yaml` |
| `DEVICES_DIR` | `devices` |
| `DEVICE_FILE` | `main.yaml` |
| `BUILD_DIR` | `build` |
| `BUILD_LOCK_FILE` | `.mcuhome-build.lock` |
| `BUILD_REPORT_FILE` | `build-report.json` |
| `SIGNING_KEY_FILE` / `PUBLIC_KEY_FILE` | `key.pem` / `key.pub` |
| `CONFIG_SCOPES` | `("system", "user", "project")` |
| `CONFIG_ORIGINS` | `("default", "program", "system", "user", "project", "environment", "arguments")` — ascending; `program` is a value an embedding program states for a shared key |
| `OPTION_KINDS` | `("string", "path", "paths", "strings", "integer", "number", "builder", "registry")` |
| `OPTIONS` | the declared option registry |
| `BUILD_TARGETS`, `TARGET_LOCAL`, `TARGET_REMOTE`, `DEFAULT_BUILD_TARGET` | where a build runs |
| `BUILD_MODES`, `MODE_CONTAINER`, `MODE_SUBPROCESS`, `DEFAULT_BUILD_MODE` | how a local build executes |
| `LOCK_OPERATIONS` | `("build", "sign", "flash", "clean", "rename", "delete")` — append-only: a word an older version does not know is rendered by the generic refusal |
| `SECRET_KINDS` | `("main", "device", "builder", "signing")` |
| `BUILD_STEPS` | `("context", "environment", "compile")` — the ordered vocabulary `on_step` emits during a build |
| `STEP_STATUSES`, `STATUS_SUCCESS`, `STATUS_FAILURE`, `STATUS_UNSUPPORTED` | what one step answered |
| `SESSION_VERBS` | the eleven verbs of the session protocol |
| `ACTION_BUILD` | the session action a firmware build runs |
| `CACHE_TIERS` | `("local", "session", "project", "shared")` |
| `WORKSPACE_LAYERS` | `("sdk", "zephyr", "chip", "mcuboot")` |
| `ARTIFACT_ROLES` | the roles a declared artifact may carry |
| `ROOT_OUT` | `out`, the artifact root a build writes to |
| `SIGNED_FIRMWARE_NAMES` | the unsigned/signed file-name pairs |
| `RESULT_FILE_PREFIX`, `RESULT_FILE_SUFFIX` | `result-`, `.json` |
| `PACKAGE_KINDS`, `KIND_SDK`, `KIND_WORKSPACE`, `KIND_TOOLS` | the three package kinds |
| `SDK_PACKAGE_NAME` | the SDK package's name |
| `OFFICIAL_BASE_DOMAIN` | the official package registry's base domain |
| `BUNDLED_ANCHOR_DIR` | the trust anchors shipped with this package |
| `DEFAULT_CONTAINER_REPOSITORIES` | the repositories a build environment is searched in |
| `DEFAULT_CONTAINER_PROGRAM`, `DEFAULT_CONTAINER_PIDS` | `docker`, `4096` |
| `DOCKER_HUB` | the default registry host of an image reference |
| `DEFAULT_MAX_WAIT_SECONDS` | `21600.0` — six hours |

## Options
Every option is declared once, in `OPTIONS`. The key, the environment
variable and the flag all derive from that declaration: the key is
`<area>.<name>`, the variable is `MCUHOME_` plus the key uppercased with
every `.` and `_` as `_`, and the flag is `--` plus the key with every
`.` and `_` as `-`. A variable or a flag exists exactly when the
option's channel is open. Nothing in this package reads a variable the
registry did not declare.

A **declared default** is what the registry states and what
`mcuhome config print` shows with the origin `default`. A **derived
fallback** is what the consumer does with a value nobody set; it is never
written into the registry, because a key that carries its consumer's
fallback looks configured when it is not. Both are listed below.

In a configuration file the area is a section:

```yaml
build:
  mode: subprocess
  sdk_sources:
    - /srv/mcuhome/packages
```

Layers, ascending: default, the system file, the user file, the project
file, the environment, the invocation's arguments. Scalars are
nearest-wins; the two map kinds merge by the name their entries are
keyed on — `builder` by builder name, `registry` by base domain.

| Key | Kind | Declared default | Derived when unset | f/e/a | Environment variable | Flag |
|---|---|---|---|---|---|---|
| `project.dir` | path | – | the upward marker search from the working directory | n/y/y | `MCUHOME_PROJECT_DIR` | `--project-dir` |
| `signing.key` | path | – | the project's `secrets/signing/key.yaml` reference | n/y/y | `MCUHOME_SIGNING_KEY` | `--signing-key` |
| `signing.imgtool` | string | – | the `imgtool` of this package's environment, then `PATH` | y/y/y | `MCUHOME_SIGNING_IMGTOOL` | `--signing-imgtool` |
| `build.target` | string (`local`, `remote`) | `local` | – | y/y/y | `MCUHOME_BUILD_TARGET` | `--build-target` |
| `build.mode` | string (`container`, `subprocess`) | `container` | – | y/y/y | `MCUHOME_BUILD_MODE` | `--build-mode` |
| `build.builder` | string | – | the built-in `local` builder | y/y/n | `MCUHOME_BUILD_BUILDER` | – |
| `build.container_program` | string | `docker` | – | y/y/y | `MCUHOME_BUILD_CONTAINER_PROGRAM` | `--build-container-program` |
| `build.container_repositories` | strings | the official build-environment repository | – | y/y/y | `MCUHOME_BUILD_CONTAINER_REPOSITORIES` | `--build-container-repositories` |
| `build.cpus` | number (> 0) | – | every core of the machine | y/y/y | `MCUHOME_BUILD_CPUS` | `--build-cpus` |
| `build.memory` | string (bytes or `k`/`m`/`g`) | – | whatever is free | y/y/y | `MCUHOME_BUILD_MEMORY` | `--build-memory` |
| `build.env_store` | path | – | `${XDG_CACHE_HOME:-~/.cache}/mcuhome/build-environments` | y/y/y | `MCUHOME_BUILD_ENV_STORE` | `--build-env-store` |
| `build.dev_workspace` | path | – | – | y/y/y | `MCUHOME_BUILD_DEV_WORKSPACE` | `--build-dev-workspace` |
| `build.python` | string | – | the interpreter this package runs on | y/y/y | `MCUHOME_BUILD_PYTHON` | `--build-python` |
| `build.sdk_sources` | paths | `()` | – | y/y/y | `MCUHOME_BUILD_SDK_SOURCES` | `--build-sdk-sources` |
| `build.workspace_sources` | paths | `()` | – | y/y/y | `MCUHOME_BUILD_WORKSPACE_SOURCES` | `--build-workspace-sources` |
| `build.tools_sources` | paths | `()` | – | y/y/y | `MCUHOME_BUILD_TOOLS_SOURCES` | `--build-tools-sources` |
| `build.sdk_max_bytes` | integer (≥ 1) | the SDK extraction bound | – | y/y/y | `MCUHOME_BUILD_SDK_MAX_BYTES` | `--build-sdk-max-bytes` |
| `build.workspace_max_bytes` | integer (≥ 1) | the workspace extraction bound | – | y/y/y | `MCUHOME_BUILD_WORKSPACE_MAX_BYTES` | `--build-workspace-max-bytes` |
| `build.tools_max_bytes` | integer (≥ 1) | the tools extraction bound | – | y/y/y | `MCUHOME_BUILD_TOOLS_MAX_BYTES` | `--build-tools-max-bytes` |
| `build.cache_root` | path | – | the user's cache directory (`XDG_CACHE_HOME`, `LOCALAPPDATA` on Windows) | y/y/y | `MCUHOME_BUILD_CACHE_ROOT` | `--build-cache-root` |
| `build.cache_local` | path | – | `<cache root>/cache-local` | y/y/y | `MCUHOME_BUILD_CACHE_LOCAL` | `--build-cache-local` |
| `build.cache_shared` | path | – | `<cache root>/cache-shared` | y/y/y | `MCUHOME_BUILD_CACHE_SHARED` | `--build-cache-shared` |
| `build.cache_session` | path | – | – (no session tier) | y/y/y | `MCUHOME_BUILD_CACHE_SESSION` | `--build-cache-session` |
| `build.cache_project` | path | – | – (no project tier) | y/y/y | `MCUHOME_BUILD_CACHE_PROJECT` | `--build-cache-project` |
| `builder.<name>.target` | string (`local`, `remote`) | – | – | y/n/n | – | – |
| `builder.<name>.server` | string | – | – | y/n/n | – | – |
| `builder.<name>.container_image` | string | – | – | y/n/n | – | – |
| `registry.<base-domain>.untrusted` | bool | `false` | – | y/n/n | – | – |
| `registry.<base-domain>.mirrors.<source>` | list | – | the mirror list the source serves | y/n/n | – | – |
| `registry.<base-domain>.anchor` | path | – | the project's `secrets/trust-anchor/<base-domain>.json` | y/n/n | – | – |

`project.dir` is the bootstrap option: it is resolved before the merge,
by `resolve_project`, and a configuration file that sets it is refused.
The two map kinds are file-level only — an environment spelling of
`registry.<base-domain>.mirrors.<source>` would be a second grammar for
a value nobody sets per invocation — and **`builder` is a reserved
area**: no option in it ever has an environment channel, so that
`MCUHOME_BUILDER_*` stays what this package sets for a build environment
it starts.

The three package-source keys are one rule: each names the directories
searched for packages of **its own kind**, and a kind is never looked for
under another kind's key. An unset key means there is no operator
directory for that kind and the package is looked for in the registry; a
machine that keeps every package in one directory names that directory in
all three keys, which is the statement it is actually making.

A **list** option is one key: its flag is repeatable and each use appends
(`--build-sdk-sources /a --build-sdk-sources /b`), and its
environment value is one string separated by the kind's separator —
`paths` by `os.pathsep`, `strings` by a comma. The flag keeps the key's
plural name: the flag *is* the key, and repeating it says "another one". A **boolean**
option derives both `--<flag>` and `--no-<flag>`, and its environment
value is `1` or `0`. No option has a short form or a second spelling.

A program that embeds this package may state its own value for a shared
key — a build server's memory budget, say — in the **program layer**,
directly above the registry default and below every file. Such a value
carries the origin `program` and the program's name as its source, so
`mcuhome config print` can account for it.

## Environment variables
Three families, and the prefix tells them apart.

**Options.** `MCUHOME_<AREA>_<NAME>`, one per option whose environment
channel is open — the table above lists every one. They are read in the
configuration layer and nowhere else.

**What MCUHome sets for a build environment it starts.**
`MCUHOME_BUILDER_<NAME>`; never an option, never read as one.
`MCUHOME_BUILDER_BASE_DIR` is the variable the build-environment
specification defines; `MCUHOME_BUILDER_TOOLS` and
`MCUHOME_BUILDER_WORKSPACE` name the two unpacked environment packages
for a build that runs without a container. A subprocess build also sets
`PATH`, `HOME`, `GIT_CONFIG_GLOBAL` and the `CCACHE_*` variables of the
compiler cache.

**Host facts this package reads from the environment mapping it was
given** — never from the process: `PATH`, `HOME`, `XDG_CONFIG_HOME`,
`XDG_CONFIG_DIRS`, `XDG_CACHE_HOME`, and on Windows `APPDATA`,
`ProgramData` and `LOCALAPPDATA` (the compiler cache goes under the local
one, because the roaming profile is the wrong place for gigabytes).
`PYTHONPATH` is read and extended for a build against a developer's own
workspace, and passed to that build alone.

## Files and directories
| What | Where | Format |
|---|---|---|
| system configuration | `/etc/mcuhome/configuration.yaml` (or the first `XDG_CONFIG_DIRS` entry; `%ProgramData%\mcuhome` on Windows) | YAML |
| user configuration | `$XDG_CONFIG_HOME/mcuhome/configuration.yaml` (`%APPDATA%\mcuhome` on Windows) | YAML |
| project configuration | `<project>/mcuhome.yaml` | YAML |
| project marker | `<project>/.mcuhome-project-root` | TOML |
| upgrade marker | `<project>/.mcuhome-project-root.upgrade` | TOML |
| devices | `<project>/devices/<name>/main.yaml` | YAML |
| project secrets | `<project>/secrets/` (mode 0700), never committed | |
| shared secrets | `secrets/main.yaml` | YAML |
| signing key | `secrets/signing/key.pem`, its public half `key.pub`, the reference `key.yaml` | PEM, YAML |
| builder credentials | `secrets/builder/<name>.yaml`, key `token` (also looked up in the user and system configuration directories) | YAML |
| device secrets | `secrets/device/<name>.yaml` | YAML |
| trust anchors | `secrets/trust-anchor/<base-domain>.json` | JSON |
| build directory | `<project>/build/<device>/` | tree |
| build lock | `<build-dir>/.mcuhome-build.lock` | flock plus a JSON record |
| build report | `<build-dir>/build-report.json` | JSON |
| build context | `build-context.json`, `context.yaml`, `manifest.yaml`, `model/device-model.json`, `keys/signing.pub`, `patches/<layer>/NNNN-*.patch` | JSON, YAML, PEM, patch |
| build environment store | `${XDG_CACHE_HOME:-~/.cache}/mcuhome/build-environments/<package>-<version>/`, each entry marked by `.mcuhome-provisioned` | tree |
| compiler cache tiers | `<cache root>/cache-local`, `<cache root>/cache-shared`, plus the session and project tiers where they are configured | directories |
| developer-workspace records | `workspace.json` and `build-workspace.json` in a build against a developer's own west workspace | JSON, in the build environment's own format |
| ignore file | `<project>/.gitignore`, written once by `create_project` with `secrets/` and `build/` | text |

Names of files and directories are lowercase with hyphens. Anything this
package writes for its own bookkeeping inside a directory that belongs to
the user is hidden and prefixed `.mcuhome-`; what a user takes away has a
plain name.

Two names in this table are another program's and are kept as that
program spells them: the compiler cache directory a build writes into is
`ccache`, after the tool that owns it, and the two developer-workspace
records carry the keys the build environment reads. A project has **no**
`configs/` directory: a device is one file plus its secrets, and shared
configuration fragments are not part of this surface.

## Documents
Keys are lowercase with underscores. A document carries every key it
declares; an absent value is `null`, `[]` or `{}`.

`ValidationResult.to_dict()` — one `diagnostics` list holds the errors
and the warnings, each with its severity, so a client renders one list:
```json
{
  "ok": true, "file": "devices/thermostat/main.yaml",
  "diagnostics": [
    {"severity": "warning", "message": "secrets/main.yaml is readable by other users",
     "file": "secrets/main.yaml", "line": null, "column": null, "key": null,
     "hint": "chmod 600 secrets/main.yaml", "kind": "exposed_secret_file"}
  ],
  "model": {}
}
```

`BuildResult.to_dict()`:
```json
{
  "ok": true, "stopped": false, "target": "local",
  "device": "thermostat", "context_id": "…",
  "out_dir": "/…/build/thermostat", "report": "build-report.json",
  "container_image": "ghcr.io/mcu-home/build-environment@sha256:…",
  "artifacts": [{"root": "out", "path": "firmware.bin", "role": "firmware", "sha256": "…"}]
}
```

`StepResult.to_dict()`:
```json
{
  "ok": true, "status": "success", "action": "build", "context_id": "…",
  "exit_code": 0, "problems": [], "violation": null,
  "artifacts": [], "out_dir": "/…/out"
}
```

`SigningResult.to_dict()`:
```json
{
  "ok": true, "out_dir": "/…/build/thermostat",
  "report_path": "/…/build/thermostat/build-report.json",
  "key": "/…/secrets/signing/key.pem",
  "signed": [{"format": "bin", "path": "/…/firmware.signed.bin"}]
}
```

`HostCheckResult.to_dict()`:
```json
{
  "ok": false,
  "findings": [
    {"check": "container_runtime", "ok": false,
     "detail": "docker is not on PATH", "hint": "install a container runtime…"}
  ]
}
```

`Settings.to_dict()` answers one entry per declared option, in
declaration order:
```json
{"build.mode": {"value": "subprocess", "origin": "project",
                "source": "/…/mcuhome.yaml"}}
```

`Diagnostic.to_dict()`: `{severity, message, file, line, column, key,
hint, kind}` — the error document's keys plus the severity, so the two
are one shape.
`BuildRecord.to_dict()`: `{out_dir, device, context_id, artifacts,
report, signed, container_image, busy}`.
`ContextVerification.to_dict()`: `{ok, root, context_id, actual_id,
mismatches: [{path, declared_sha256, actual_sha256}]}`.
`SignPlan.to_dict()`: `{out_dir, report_path, key, commands: [{format,
argv, output}]}`.
`Builder.to_dict()`: `{name, target, origin, source, server,
container_image}` — `origin` is the layer that defined the entry and
`source` the file it came from, the same pair `Setting` uses.
`SelectedBuilder.to_dict()`: `{target, builder, server, container_image}`
— the token is a secret and is in no document.
`RegistrySettings.to_dict()`: `{base_domain, untrusted, anchor, mirrors}`.
`Artifact.to_dict()`: `{root, path, role, sha256}`.

On-disk records this package writes and reads:
- project marker (TOML): `version`, `id`, and `[upgrade]` with `started`,
  `process`, `host`, `running`.
- build lock (JSON inside a flocked file): `pid`, `host`, `device`,
  `operation`, `started`.
- store marker `.mcuhome-provisioned` (JSON): `kind`, `package`,
  `version`, `sha256`, `provisioned`.

The build report and the build-context files are formats of the
build-environment specification; this package reads and writes them but
does not define them.

## Names re-exported from the device-model package
These come from `mcuhome.model`, which the build environment itself uses,
and are re-exported here unchanged so that a consumer needs one import.
Four are renamed because the bare name would say nothing in this
namespace: `device_registry` (the model's `registry_data`),
`parse_container_reference` (its `parse_reference`),
`MODEL_PACKAGE_VERSION` (its `__version__`) and `expand_user_path` (its
`expand`). The last of the four is a thin **wrapper** and not an alias:
it takes `env` keyword-only, because every parameter on this surface
follows the same rule. Nothing else is wrapped.

`DeviceModel`, `read_model`, `to_json`, `Artifact`, `BuildLimits`,
`Location`, `MCUHomeError`, `ConfigError`, `ConfigErrorGroup`,
`GenerationError`, `BuildError`, `error_dicts`, `Pairing`,
`random_pairing`, `PairingModel`, `OtaImage`, `OtaIdentity`,
`ota_parameters`, `BOARDS`, `PLANNED_BOARDS`, `CLUSTERS`, `BoardDef`,
`ClusterDef`, `PartitionDef`, `UpdateSchemeDef`, `sha256_file`,
`SDK_PACKAGE_NAME`, `DOCKER_HUB`, `parse_container_reference`,
`Declaration`, `PackageMember`, `LABEL_PREFIX`, `SPEC_GENERATION_MEMBER`,
`ENVIRONMENT_IMAGE_REPOSITORY`, `BUILD_CONTEXT_FILE`, `CONTEXT_FILE`,
`MANIFEST_FILE`, `MODEL_FILE`, `KEYS_DIR`, `PATCHES_DIR`,
`DEVELOPER_ENVIRONMENT`, `ContextFile`, `ContextManifest`,
`ContextRequest`, `ContextEnvironment`, `EnvironmentPin`, `SdkPin`,
`DeveloperEnvironment`, `GeneratorEntry`, `PackagePin`, `context_id`,
`format_generator_chain`.

## What is not public
Everything under `mcuhome.workbench` that is not reachable from
`mcuhome.workbench.api`. Module layout, helper functions, the
compositions behind the build targets, the session protocol client and
the container command line are implementation detail and change without
notice. A program that imports them is not covered by anything in this
document.

The re-exported device-model names track exactly the `mcuhome.model`
version range this package declares as its dependency in
`pyproject.toml`; that range is what an embedder pins beside this
package, and a model release outside it is not covered by this document.
`MODEL_PACKAGE_VERSION` answers which one is installed, `MODEL_VERSION`
the model format it writes.

MCUHome's own test suites are the one place that reaches past this
surface on purpose, and only where no exported name can express the
test: the container layout constants of one execution profile
(`ENTRY_POINT_PATH`, `REQUEST_TARGET`, `OUT_TARGET`), the runtime and
registry objects a test replaces wholesale, and the private helpers a
test monkeypatches. Any seam that has an exported equivalent is used
through the exported one.

Two consequences worth stating:
- There is no supported way to reach the private half of a signing key
  through a build. A build context carries `keys/signing.pub` and nothing
  else of the pair.
- The `detail` attribute of a `BuildResult` is the composition's own
  object. It is useful in a log and is not part of any document; its
  shape is not covered here.

## Index of exported names
Everything `mcuhome.workbench.api` exports, in one list. Nothing else in
this package is public.

**Projects and devices** — `resolve_project`, `read_project`,
`create_project`, `find_project_root`, `is_project_root`, `is_upgrading`,
`resolve_device`, `create_device`, `render_device_file`, `create_pairing`,
`read_pairing`, `rename_device`, `delete_device`, `require_secret_file`,
`read_yaml_file`, `find_secret_scopes`, `read_secrets`, `reveal_secret`,
`set_secret`, `unset_secret`, `delete_secret_file`, `SecretScope`,
`SecretKey`, `SecretFile`, `Project`,
`ProjectFile`, `UpgradeRecord`, `NewProject`, `NewDevice`, `NewPairing`,
`DeviceOutline`, `BusChoice`, `PeripheralChoice`, `EndpointChoice`,
`ClusterChoice`.

**Device models** — `load_model`, `validate_device`, `read_model`,
`generate_application`, `device_schema`, `device_registry`, `to_json`,
`error_dicts`, `expand_user_path`, `ValidationResult`, `Diagnostic`,
`DeviceModel`, `Location`.

**Configuration** — `resolve_settings`, `option`, `resolve_config_file`,
`set_config_value`, `unset_config_value`, `resolve_build_options`,
`Argument`, `ProgramDefaults`, `Option`, `Setting`, `Settings`,
`BuildOptions`.

**Builders** — `resolve_builder`, `Builder`, `SelectedBuilder`.

**Building firmware** — `build_firmware`, `resolve_build_target`,
`resolve_build_mode`, `open_build_lock`, `is_busy`, `read_build`,
`clean_build`, `check_build_host`, `build_steps`, `BuildRequest`, `BuildResult`,
`BuildRecord`, `BuildTarget`, `LocalBuild`, `RemoteBuild`, `Execution`,
`ContainerExecution`, `SubprocessExecution`, `HostFinding`,
`HostCheckResult`, `SeatWait`, `Artifact`, `BuildLimits`.

**Build contexts** — `create_context`, `lock_context`, `verify_context`,
`read_context_manifest`, `read_generator_chain`, `read_context_facts`,
`ContextVerification`, `FileMismatch`, `ContextRequest`,
`ContextManifest`, `ContextFile`, `ContextEnvironment`, `EnvironmentPin`,
`SdkPin`, `PackagePin`, `DeveloperEnvironment`, `GeneratorEntry`,
`context_id`, `format_generator_chain`.

**Build environments** — `provision_environment`, `open_builder_session`,
`create_launcher`, `resolve_container_program`,
`require_container_runtime`, `require_container_image`,
`ensure_container_image`, `resolve_container_image`,
`parse_container_image`, `parse_container_reference`,
`resolve_cache_tiers`, `resolve_cache_root`, `resolve_host_limits`,
`parse_memory`, `resolve_shutdown_seconds`, `current_user`,
`BuilderSession`, `Step`, `StepResult`, `CacheTier`, `Liveness`,
`Launcher`, `StoreEntry`, `ContainerRuntime`, `ContainerLimits`,
`ContainerImagePin`, `ContainerImageMatch`, `ImageRegistry`,
`Declaration`, `PackageMember`.

**Packages and registries** — `open_package_registry`,
`fetch_sdk_package`, `resolve_package`, `sha256_file`, `RegistrySource`,
`RegistrySettings`, `AcquiredPackage`, `ResolvedPackage`.

**Signing, reports and OTA** — `resolve_signing_key`,
`create_signing_key`, `generate_key_pem`, `public_key_pem`,
`is_p256_private_key`, `is_p256_public_key`, `read_build_report`,
`plan_signing`, `sign_firmware`, `write_ota_image`, `ota_file_name`,
`ota_parameters`, `SigningKey`, `SignPlan`, `SignedArtifact`,
`SigningResult`, `OtaImage`, `OtaIdentity`, `Pairing`, `random_pairing`,
`PairingModel`.

**Upgrading a project** — `open_upgrade_session`, `plan_upgrade`,
`find_running_builds`, `UpgradeSession`, `UpgradeResult`, `Migration`,
`RunningBuild`.

**Hardware and Matter tables** — `BOARDS`, `PLANNED_BOARDS`, `CLUSTERS`,
`BoardDef`, `ClusterDef`, `PartitionDef`, `UpdateSchemeDef`.

**Exceptions** — `MCUHomeError`, `ConfigError`, `ConfigErrorGroup`,
`GenerationError`, `BuildError`, `CompilerUnavailable`,
`BuildDirectoryBusy`, `SdkUnavailable`, `EnvironmentUnavailable`,
`EnvironmentUnusable`, `BuildEnvironmentError`,
`ContextFormatVersionError`, `PackageRegistryError`, `TrustAnchorMissing`,
`ImageRegistryError`, `ImageRegistryUnauthorized`,
`ImageRegistryUnreachable`, `UnknownBuildTarget`, `UnknownBuildMode`,
`RemoteNotConfigured`, `RemoteError`, `RemoteDependencyMissing`,
`RemoteTransportError`, `ServerRefusal`, `WaitedTooLong`,
`ContextIdMismatch`, `ContextTooLarge`, `PrivateKeyRefused`,
`ProjectFileError`, `ProjectUpgradeRequired`, `ProjectVersionUnsupported`,
`UpgradeInProgress`, `UpgradeInterrupted`, `MigrationFailed`.

**Constants** — `VERSION`, `MODEL_VERSION`, `MODEL_PACKAGE_VERSION`,
`PROJECT_VERSION`, `SPEC_GENERATION`, `PROJECT_MARKER_FILE`,
`UPGRADE_MARKER_FILE`, `PROJECT_CONFIG_FILE`, `CONFIG_FILE`,
`DEVICES_DIR`, `DEVICE_FILE`, `BUILD_DIR`, `BUILD_LOCK_FILE`,
`BUILD_REPORT_FILE`, `SIGNING_KEY_FILE`, `PUBLIC_KEY_FILE`,
`CONFIG_SCOPES`, `CONFIG_ORIGINS`, `OPTION_KINDS`, `OPTIONS`,
`BUILD_TARGETS`, `TARGET_LOCAL`, `TARGET_REMOTE`, `DEFAULT_BUILD_TARGET`,
`BUILD_MODES`, `MODE_CONTAINER`, `MODE_SUBPROCESS`, `DEFAULT_BUILD_MODE`,
`LOCK_OPERATIONS`, `SECRET_KINDS`, `BUILD_STEPS`, `STEP_STATUSES`,
`STATUS_SUCCESS`, `STATUS_FAILURE`,
`STATUS_UNSUPPORTED`, `SESSION_VERBS`, `ACTION_BUILD`, `CACHE_TIERS`,
`WORKSPACE_LAYERS`, `ARTIFACT_ROLES`, `ROOT_OUT`, `SIGNED_FIRMWARE_NAMES`,
`RESULT_FILE_PREFIX`, `RESULT_FILE_SUFFIX`, `PACKAGE_KINDS`, `KIND_SDK`,
`KIND_WORKSPACE`, `KIND_TOOLS`, `SDK_PACKAGE_NAME`, `OFFICIAL_BASE_DOMAIN`,
`BUNDLED_ANCHOR_DIR`, `DEFAULT_CONTAINER_REPOSITORIES`,
`DEFAULT_CONTAINER_PROGRAM`, `DEFAULT_CONTAINER_PIDS`, `DOCKER_HUB`,
`DEFAULT_MAX_WAIT_SECONDS`, `BUILD_CONTEXT_FILE`, `CONTEXT_FILE`,
`MANIFEST_FILE`, `MODEL_FILE`, `KEYS_DIR`, `PATCHES_DIR`,
`DEVELOPER_ENVIRONMENT`, `LABEL_PREFIX`, `SPEC_GENERATION_MEMBER`,
`ENVIRONMENT_IMAGE_REPOSITORY`.
