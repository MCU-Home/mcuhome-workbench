# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Looking at and changing a project's secrets: :mod:`mcuhome.workbench.secrets`.

Three promises are what this file exists for, and each of them is cheap
to break by accident:

* **no document carries a value** — the masks are a constant, and a
  planted secret is searched for in the JSON of every document the six
  functions answer;
* **an exposed file is refused rather than read** — for every one of the
  six, because a guard that half the surface runs is not a guard;
* **a write is a round trip** — a user's comments, order and other
  entries survive an edit, and emptying a file leaves an empty file
  rather than a ``{}`` nobody wrote.

The rest is the vocabulary: which scopes a project has, which device
reads which shared secret, and the refusals that keep a name from
becoming a path.
"""

from __future__ import annotations

import ast
import json
import os
import stat
from pathlib import Path

import pytest
from conftest import package_modules
from mcuhome.model.errors import ConfigError

from mcuhome.workbench import api
from mcuhome.workbench.secrets import MASKED_VALUE

#: The value planted wherever a test needs one that must never appear.
PLANTED = "hunter2-do-not-print"

DEVICE_CONFIG = """\
device:
  name: {name}
  board: nrf7002dk/nrf5340/cpuapp

network:
  wifi:
    ssid: bench
    password: !secret {secret}
"""


def make_project(tmp_path: Path) -> api.Project:
    """A real project directory, marker and ``secrets/`` included."""
    return api.create_project(tmp_path / "project").project


def add_device(project: api.Project, name: str, *, secret: str = "wifi_password") -> Path:
    """A device file that reads one secret, without a build behind it."""
    entry = project.device_entry(name)
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text(DEVICE_CONFIG.format(name=name, secret=secret), encoding="utf-8")
    return entry


def write_secrets(path: Path, text: str, *, mode: int = 0o600) -> Path:
    """A secrets file as a user's editor would leave it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)
    return path


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# --------------------------------------------------------------------------
# The scopes
# --------------------------------------------------------------------------


def test_a_fresh_project_has_the_two_scopes_it_always_has(tmp_path: Path) -> None:
    """``main`` and ``signing`` are scopes before anything is in them."""
    project = make_project(tmp_path)

    scopes = api.find_secret_scopes(project)

    assert [(scope.kind, scope.name, scope.exists) for scope in scopes] == [
        ("main", "", False),
        ("signing", "", False),
    ]
    assert scopes[0].file == project.secrets_file


def test_the_scopes_of_a_project_that_has_everything(tmp_path: Path) -> None:
    """One scope per device and per builder file, in the order of the kinds.

    The device without a secrets file is listed as much as the one with
    it — a client offers "add a secret for this device" for it — and so
    is a file left behind by a device that no longer exists, which is
    the only way it can ever be found and removed.
    """
    project = make_project(tmp_path)
    add_device(project, "thermostat")
    add_device(project, "kitchen")
    write_secrets(project.device_secrets_file("thermostat"), "passcode: 20202021\n")
    write_secrets(project.device_secrets_file("ghost"), "passcode: 1\n")
    write_secrets(project.builder_secrets_file("attic"), "token: t\n")
    write_secrets(project.secrets_file, "wifi_password: x\n")

    scopes = api.find_secret_scopes(project)

    assert [(scope.kind, scope.name, scope.exists) for scope in scopes] == [
        ("main", "", True),
        ("device", "ghost", True),
        ("device", "kitchen", False),
        ("device", "thermostat", True),
        ("builder", "attic", True),
        ("signing", "", False),
    ]
    assert every_kind_is_published(scopes)


def every_kind_is_published(scopes: tuple[api.SecretScope, ...]) -> bool:
    return all(scope.kind in api.SECRET_KINDS for scope in scopes)


def test_a_scope_is_not_a_second_spelling_of_a_path(tmp_path: Path) -> None:
    """A name becomes a file name, so it is one plain word or a refusal."""
    project = make_project(tmp_path)
    add_device(project, "thermostat")

    for name in ("../../etc/passwd", "sub/dir", ".hidden", ""):
        with pytest.raises(ConfigError):
            api.read_secrets(project, kind="builder", name=name)

    with pytest.raises(ConfigError) as caught:
        api.read_secrets(project, kind="device", name="ghost")
    assert "thermostat" in (caught.value.hint or ""), "a refusal names the devices there are"

    with pytest.raises(ConfigError):
        api.read_secrets(project, kind="vault")
    with pytest.raises(ConfigError):
        api.read_secrets(project, kind="main", name="thermostat")
    with pytest.raises(ConfigError):
        api.read_secrets(project, kind="device")


# --------------------------------------------------------------------------
# Reading — and what a document may carry
# --------------------------------------------------------------------------


def test_reading_a_file_answers_keys_and_never_a_value(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    write_secrets(
        project.secrets_file,
        f"# my secrets\nwifi_password: {PLANTED}\napi_token: {PLANTED}-2\n",
    )

    file = api.read_secrets(project, kind="main")

    assert [key.key for key in file.keys] == ["wifi_password", "api_token"]
    assert {key.masked for key in file.keys} == {MASKED_VALUE}
    assert all(key.masked != PLANTED for key in file.keys)
    assert PLANTED not in json.dumps(file.to_dict())


def test_the_mask_says_nothing_about_the_value(tmp_path: Path) -> None:
    """Two different values mask the same, and neither is the length.

    A mask derived from the value — its length, its first character,
    whether two entries hold the same thing — is part of the value, and
    part of a secret is a secret.
    """
    project = make_project(tmp_path)
    write_secrets(project.secrets_file, "short: a\nlong: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n")

    file = api.read_secrets(project, kind="main")

    masks = {key.key: key.masked for key in file.keys}
    assert masks["short"] == masks["long"]
    assert len(masks["short"]) != 1


def test_no_document_any_secrets_function_answers_carries_a_value(tmp_path: Path) -> None:
    """The whole surface at once, against one planted value.

    Every document the six functions answer is rendered as JSON and
    searched. ``reveal_secret`` is the one call that answers a value, and
    it answers a bare string rather than a document — which is exactly
    the line this test draws.
    """
    project = make_project(tmp_path)
    add_device(project, "thermostat")
    write_secrets(project.secrets_file, f"wifi_password: {PLANTED}\n")
    write_secrets(project.device_secrets_file("thermostat"), f"passcode: {PLANTED}-p\n")
    write_secrets(project.builder_secrets_file("attic"), f"token: {PLANTED}-t\n")

    documents = [
        [scope.to_dict() for scope in api.find_secret_scopes(project)],
        api.read_secrets(project, kind="main").to_dict(),
        api.read_secrets(project, kind="device", name="thermostat").to_dict(),
        api.read_secrets(project, kind="builder", name="attic").to_dict(),
        api.read_secrets(project, kind="signing").to_dict(),
    ]
    api.set_secret(project, kind="main", key="another", value=f"{PLANTED}-s")
    documents.append(api.read_secrets(project, kind="main").to_dict())

    rendered = json.dumps(documents)
    assert PLANTED not in rendered
    assert api.reveal_secret(project, kind="main", key="wifi_password") == PLANTED


def test_reveal_answers_exactly_the_one_key_that_was_asked_for(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    write_secrets(project.secrets_file, f"wifi_password: {PLANTED}\napi_token: other\n")

    assert api.reveal_secret(project, kind="main", key="wifi_password") == PLANTED

    with pytest.raises(ConfigError) as caught:
        api.reveal_secret(project, kind="main", key="nothing_like_it")
    assert "wifi_password" in (caught.value.hint or "")
    assert PLANTED not in (caught.value.message + (caught.value.hint or "")), (
        "a refusal about a missing key must not print the keys that are there"
    )


def test_reveal_speaks_yaml_spelling_for_what_is_not_a_string(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    write_secrets(project.secrets_file, "passcode: 20202021\nquiet: true\nempty:\n")

    assert api.reveal_secret(project, kind="main", key="passcode") == "20202021"
    assert api.reveal_secret(project, kind="main", key="quiet") == "true"
    assert api.reveal_secret(project, kind="main", key="empty") == ""


def test_reading_a_scope_with_no_file_is_not_a_refusal(tmp_path: Path) -> None:
    """A client opens what it listed; ``reveal`` is the one that refuses."""
    project = make_project(tmp_path)
    add_device(project, "thermostat")

    file = api.read_secrets(project, kind="device", name="thermostat")

    assert file.keys == ()
    assert file.scope.exists is False
    with pytest.raises(ConfigError):
        api.reveal_secret(project, kind="device", name="thermostat", key="passcode")


# --------------------------------------------------------------------------
# used_by
# --------------------------------------------------------------------------


def test_used_by_names_the_devices_whose_references_reach_the_entry(tmp_path: Path) -> None:
    """The ladder decides: a device's own file shadows the shared one.

    ``kitchen`` names ``wifi_password`` and has no file of its own, so it
    reads the shared entry. ``thermostat`` names the same secret and
    defines it itself, so the shared entry is not what it uses — which is
    the difference between "who mentions this word" and "who reads this
    value".
    """
    project = make_project(tmp_path)
    add_device(project, "kitchen")
    add_device(project, "thermostat")
    write_secrets(project.secrets_file, "wifi_password: x\nunused: y\n")
    write_secrets(project.device_secrets_file("thermostat"), "wifi_password: own\n")

    shared = api.read_secrets(project, kind="main")

    used = {key.key: key.used_by for key in shared.keys}
    assert used == {"wifi_password": ("kitchen",), "unused": ()}

    own = api.read_secrets(project, kind="device", name="thermostat")
    assert [key.used_by for key in own.keys] == [("thermostat",)]


def test_a_device_file_that_cannot_be_parsed_is_not_reported_here(tmp_path: Path) -> None:
    """A broken configuration is the validator's to report, not this list."""
    project = make_project(tmp_path)
    add_device(project, "kitchen")
    broken = project.device_entry("broken")
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_text("device: [unclosed\n", encoding="utf-8")
    write_secrets(project.secrets_file, "wifi_password: x\n")

    shared = api.read_secrets(project, kind="main")

    assert [key.used_by for key in shared.keys] == [("kitchen",)]


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------


def test_the_first_secret_creates_the_file_owner_only(tmp_path: Path) -> None:
    project = make_project(tmp_path)

    api.set_secret(project, kind="main", key="wifi_password", value=PLANTED)

    file = project.secrets_file
    assert file.is_file()
    assert mode_of(file) == 0o600
    assert mode_of(project.secrets_dir) == 0o700
    assert api.reveal_secret(project, kind="main", key="wifi_password") == PLANTED


def test_a_builder_file_is_created_with_its_directory(tmp_path: Path) -> None:
    """The directory on the way to a new secrets file is owner-only too."""
    project = make_project(tmp_path)

    api.set_secret(project, kind="builder", name="attic", key="token", value="t")

    file = project.builder_secrets_file("attic")
    assert mode_of(file) == 0o600
    assert mode_of(file.parent) == 0o700


def test_setting_a_secret_leaves_the_rest_of_the_file_exactly_as_it_was(tmp_path: Path) -> None:
    """The round trip: comments, order, quoting and blank lines survive."""
    project = make_project(tmp_path)
    original = (
        "# The project's shared secrets.\n"
        "\n"
        "wifi_password: 'keep me'   # the one the router wants\n"
        "\n"
        "# the token nobody has rotated yet\n"
        "api_token: abc\n"
    )
    write_secrets(project.secrets_file, original)

    api.set_secret(project, kind="main", key="api_token", value="rotated")
    api.set_secret(project, kind="main", key="new_one", value="fresh")

    written = project.secrets_file.read_text(encoding="utf-8")
    assert written == original.replace("api_token: abc", "api_token: rotated") + "new_one: fresh\n"
    assert mode_of(project.secrets_file) == 0o600


def test_unsetting_the_last_secret_leaves_an_empty_file(tmp_path: Path) -> None:
    """Not a deleted file, and not a ``{}`` the user would have to look up."""
    project = make_project(tmp_path)
    write_secrets(project.secrets_file, "# my secrets\nwifi_password: x\n")

    assert api.unset_secret(project, kind="main", key="wifi_password") is True

    written = project.secrets_file.read_text(encoding="utf-8")
    assert project.secrets_file.is_file()
    assert "{}" not in written
    assert written.strip() == "# my secrets"
    assert api.read_secrets(project, kind="main").keys == ()


def test_unsetting_what_is_not_there_answers_false_and_writes_nothing(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    original = "wifi_password: x\n"
    write_secrets(project.secrets_file, original)

    add_device(project, "kitchen")
    assert api.unset_secret(project, kind="main", key="never_set") is False
    assert api.unset_secret(project, kind="device", name="kitchen", key="x") is False
    assert project.secrets_file.read_text(encoding="utf-8") == original


def test_a_deleted_file_is_a_whole_scope_and_only_a_named_one(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    add_device(project, "thermostat")
    write_secrets(project.device_secrets_file("thermostat"), "passcode: 1\n")
    write_secrets(project.builder_secrets_file("attic"), "token: t\n")

    assert api.delete_secret_file(project, kind="device", name="thermostat") is True
    assert api.delete_secret_file(project, kind="device", name="thermostat") is False
    assert api.delete_secret_file(project, kind="builder", name="attic") is True
    assert not project.device_secrets_file("thermostat").exists()


def test_the_projects_own_files_are_never_deleted_as_a_file(tmp_path: Path) -> None:
    """``main`` and ``signing`` are emptied key by key, never removed."""
    project = make_project(tmp_path)
    write_secrets(project.secrets_file, "wifi_password: x\n")
    write_secrets(project.firmware_secrets_file, "firmware_signing_key: !file key.pem\n")

    for kind in ("main", "signing"):
        with pytest.raises(ConfigError) as caught:
            api.delete_secret_file(project, kind=kind, name="")
        assert "emptied" in (caught.value.hint or "")

    assert project.secrets_file.is_file()
    assert project.firmware_secrets_file.is_file()


# --------------------------------------------------------------------------
# The signing scope
# --------------------------------------------------------------------------


def signing_project(tmp_path: Path) -> api.Project:
    """A project with a signing key the way ``create_signing_key`` leaves it."""
    project = make_project(tmp_path)
    api.create_signing_key(env={}, project=project)
    return project


def test_the_signing_scope_answers_presence_and_not_a_key(tmp_path: Path) -> None:
    """The entry that points at the key, with no byte of the key in sight."""
    project = signing_project(tmp_path)
    key = api.resolve_signing_key(env={}, project=project)
    assert "PRIVATE KEY" in key.pem, "the fixture has to be a real key for this to mean anything"

    file = api.read_secrets(project, kind="signing")

    assert [entry.key for entry in file.keys] == ["firmware_signing_key"]
    assert file.scope.exists is True
    assert file.scope.file == project.firmware_secrets_file
    document = json.dumps(file.to_dict())
    assert "PRIVATE KEY" not in document
    assert key.pem.splitlines()[1] not in document


def test_a_key_file_reference_is_never_revealed(tmp_path: Path) -> None:
    project = signing_project(tmp_path)

    with pytest.raises(ConfigError) as caught:
        api.reveal_secret(project, kind="signing", key="firmware_signing_key")

    assert "points at a file" in caught.value.message
    assert "PRIVATE KEY" not in (caught.value.message + (caught.value.hint or ""))


def test_key_material_is_drawn_and_not_typed_in(tmp_path: Path) -> None:
    """``set_secret`` refuses the signing scope and says what draws a key."""
    project = signing_project(tmp_path)
    before = project.firmware_secrets_file.read_text(encoding="utf-8")

    with pytest.raises(ConfigError) as caught:
        api.set_secret(project, kind="signing", key="firmware_signing_key", value="-----BEGIN…")

    assert "--signing-key" in (caught.value.hint or "")
    assert project.firmware_secrets_file.read_text(encoding="utf-8") == before

    # And the same refusal wherever an entry references a file, whatever
    # scope it is in: replacing a reference with a value would unhook key
    # material that is still on disk.
    write_secrets(project.secrets_file, "certificate: !file cert.pem\n")
    (project.secrets_dir / "cert.pem").write_text("not a secret\n", encoding="utf-8")
    with pytest.raises(ConfigError) as caught:
        api.set_secret(project, kind="main", key="certificate", value="inline")
    assert "points at a file" in caught.value.message


def test_the_signing_reference_can_still_be_removed(tmp_path: Path) -> None:
    """The project's own files are emptied key by key — this is that key."""
    project = signing_project(tmp_path)
    key_file = api.resolve_signing_key(env={}, project=project).path

    assert api.unset_secret(project, kind="signing", key="firmware_signing_key") is True

    assert "{}" not in project.firmware_secrets_file.read_text(encoding="utf-8")
    assert key_file.is_file(), "the key file is not removed by an edit of the YAML"


# --------------------------------------------------------------------------
# The permission guard
# --------------------------------------------------------------------------


@pytest.mark.skipif(os.name != "posix", reason="file modes are POSIX")
def test_every_one_of_the_six_refuses_an_exposed_file(tmp_path: Path) -> None:
    """A guard half the surface runs is not a guard.

    Each call is made against a world-readable ``secrets/main.yaml`` — or,
    for the two that take a named scope, a world-readable device file —
    and each one has to refuse before it reads or writes anything.
    """
    project = make_project(tmp_path)
    add_device(project, "thermostat")
    shared = write_secrets(project.secrets_file, f"wifi_password: {PLANTED}\n", mode=0o644)
    device = write_secrets(
        project.device_secrets_file("thermostat"), f"passcode: {PLANTED}\n", mode=0o644
    )
    before = shared.read_text(encoding="utf-8")

    calls = (
        lambda: api.find_secret_scopes(project),
        lambda: api.read_secrets(project, kind="main"),
        lambda: api.reveal_secret(project, kind="main", key="wifi_password"),
        lambda: api.set_secret(project, kind="main", key="wifi_password", value="new"),
        lambda: api.unset_secret(project, kind="main", key="wifi_password"),
        lambda: api.delete_secret_file(project, kind="device", name="thermostat"),
    )
    for call in calls:
        with pytest.raises(ConfigError) as caught:
            call()
        assert "chmod 600" in (caught.value.hint or "")

    assert shared.read_text(encoding="utf-8") == before, "the refusal came before the write"
    assert device.is_file(), "the refusal came before the delete"


@pytest.mark.skipif(os.name != "posix", reason="file modes are POSIX")
def test_a_device_file_nobody_asked_about_is_still_not_read(tmp_path: Path) -> None:
    """``used_by`` reads the other devices' files, so the guard runs there too."""
    project = make_project(tmp_path)
    add_device(project, "thermostat")
    write_secrets(project.secrets_file, "wifi_password: x\n")
    write_secrets(project.device_secrets_file("thermostat"), "passcode: 1\n", mode=0o640)

    with pytest.raises(ConfigError):
        api.read_secrets(project, kind="main")


def test_a_file_emptied_and_written_again_keeps_its_comments(tmp_path: Path) -> None:
    """The round trip survives the one state that has no mapping in it.

    A file holding nothing but comments is what ``unset_secret`` leaves
    behind, and it parses to no mapping at all — so the next
    ``set_secret`` has nothing to add to and would write over the user's
    lines if it did the obvious thing.
    """
    project = make_project(tmp_path)
    write_secrets(project.secrets_file, "# the shared secrets of this project\nwifi_password: x\n")

    api.unset_secret(project, kind="main", key="wifi_password")
    api.set_secret(project, kind="main", key="wifi_password", value="again")

    assert project.secrets_file.read_text(encoding="utf-8") == (
        "# the shared secrets of this project\nwifi_password: again\n"
    )


# --------------------------------------------------------------------------
# Who may write into secrets/
# --------------------------------------------------------------------------

#: Every place in the package **outside this surface** that writes
#: anything under a project's ``secrets/``, with what it is for. Each of
#: them is a call that brings a secret into existence in the first place;
#: changing one afterwards is the six functions' job alone, and a
#: seventh writer appearing anywhere in the package fails this test
#: instead of going unnoticed.
WRITERS = {
    "project.create_project": "creates secrets/ (mode 700) when a project is created",
    "project.ensure_secrets_dir": "creates a directory inside it, owner-only",
    "packageregistry.install_trust_anchors": "writes the bundled trust anchors",
    "provision.create_pairing": "draws a device's commissioning credentials",
    "provision._write_secrets": "and writes them into the device's own file",
    "signing._create_project_key": "draws the firmware signing key and references it",
}

#: The writers inside :mod:`~mcuhome.workbench.secrets` itself: the three
#: functions that change a file, and the one helper they share. Every
#: other function of that module reads, and this is what says so.
#: ``_render`` is in the list because it dumps — into a string buffer,
#: which the check below cannot tell from a file, and a list that left it
#: out would have to leave the dump out of the vocabulary instead.
OWN_WRITERS = ("_render", "_write", "delete_secret_file", "set_secret", "unset_secret")

#: How a path under ``secrets/`` is spelled anywhere in this package: the
#: project's own accessors and the constant they are built from. A new
#: spelling has to be added here, which is the point — a module that
#: reaches into the directory by another name is exactly what this test
#: is looking for.
SECRETS_PATHS = frozenset(
    {
        "SECRETS_DIR",
        "builder_secrets_dir",
        "builder_secrets_file",
        "device_secrets_dir",
        "device_secrets_file",
        "ensure_secrets_dir",
        "firmware_secrets_file",
        "secrets_dir",
        "secrets_file",
    }
)

#: What counts as writing: every call in the standard library's
#: vocabulary for putting something where a file was, or taking one
#: away, plus the private helpers of this package — a writer that went
#: through one of those and was not listed here would be invisible.
#: ``open`` is in the list whatever mode it is called with, because the
#: mode is an argument this check does not read, and a needless entry
#: costs one line in :data:`WRITERS` while a missing one costs the test.
WRITE_CALLS = frozenset(
    {
        "_mkdir_private",
        "_write",
        "_write_owner_only",
        "chmod",
        "chown",
        "copy",
        "copy2",
        "copyfile",
        "copytree",
        "dump",
        "hardlink_to",
        "link",
        "makedirs",
        "mkdir",
        "move",
        "open",
        "remove",
        "removedirs",
        "rename",
        "renames",
        "replace",
        "rmdir",
        "rmtree",
        "symlink",
        "symlink_to",
        "touch",
        "truncate",
        "unlink",
        "write",
        "write_bytes",
        "write_text",
        "writelines",
    }
)


def called_names(node: ast.AST) -> set[str]:
    """Every name called inside *node*, attribute calls by their attribute."""
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            if isinstance(child.func, ast.Attribute):
                names.add(child.func.attr)
            elif isinstance(child.func, ast.Name):
                names.add(child.func.id)
    return names


def mentioned_names(node: ast.AST) -> set[str]:
    """Every name and attribute *node* mentions, called or not."""
    return {child.attr for child in ast.walk(node) if isinstance(child, ast.Attribute)} | {
        child.id for child in ast.walk(node) if isinstance(child, ast.Name)
    }


def functions_of(module: Path) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [
        node
        for node in ast.walk(ast.parse(module.read_text(encoding="utf-8")))
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    ]


def test_nothing_outside_this_surface_writes_into_secrets() -> None:
    """Every function of every module is read, not only the ones we remember.

    A surface that promises "these six calls are how you change your
    secrets" is worth exactly what the rest of the package does behind
    it. A function that names a path under ``secrets/`` and writes has to
    be in :data:`WRITERS`, with a line saying why it is there.

    **What this cannot see**, stated so that nobody reads more into a
    passing run than is in it: the check is two vocabularies
    (:data:`SECRETS_PATHS`, :data:`WRITE_CALLS`) matched against one
    function's own body. A write that goes through a private helper this
    file does not name, or through a path spelled some other way — a
    string joined together, a directory handed in as an argument — is
    invisible to it. Both vocabularies are therefore kept wide rather
    than exact, and the day a writer is added behind a new helper, the
    helper goes in the list with it.
    """
    found = sorted(
        f"{module.stem}.{node.name}"
        for module in package_modules()
        if module.stem != "secrets"
        for node in functions_of(module)
        if mentioned_names(node) & SECRETS_PATHS
        if called_names(node) & WRITE_CALLS
    )

    assert found == sorted(WRITERS), (
        "the writers of secrets/ changed — add the new one to WRITERS with its reason, "
        "or route the write through the six functions"
    )


def test_only_the_three_changing_calls_write_in_this_module() -> None:
    """The other half: inside the module, reading reads and writing writes."""
    module = next(path for path in package_modules() if path.stem == "secrets")

    writers = sorted(node.name for node in functions_of(module) if called_names(node) & WRITE_CALLS)

    assert writers == sorted(OWN_WRITERS)
