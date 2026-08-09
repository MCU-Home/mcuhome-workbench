# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The west workspace baked into the builder image (r3, ADR 0020 E5).

**Docker never runs here**, same rule as ``test_container.py``: building
the image takes minutes and gigabytes, and the pytest half of the strategy
is the fast half. What is tested is everything that can drift *between*
two files without anybody noticing — the Zephyr and CHIP revisions the
``Dockerfile`` restates against the ones ``west.yml`` pins, the patch file
names it constructs from them, the paths it copies out of a build context
that is now the whole repository, and the shape of the record the image
carries.

Every assertion here has a failure mode behind it that a build would not
report as a build failure. A Zephyr bump that misses the ``Dockerfile``
gets a workspace at one revision and a tag fetch for another; a renamed
patch gets an image built without it; a moved path gets a `COPY` that
fails only when someone builds the image, which is the one thing this
suite exists not to require.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = REPO_ROOT / "containers" / "builder" / "Dockerfile"
RECORD_SCRIPT = REPO_ROOT / "containers" / "builder" / "workspace-record.py"

#: Where the workspace lives inside the image. Restated here on purpose:
#: this is the one place that says "and it must stay that", and several
#: assertions below are about the parts of the Dockerfile agreeing on it.
TOPDIR = "/mcuhome/workspace"


def _dockerfile() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


def _arg(name: str) -> str:
    """The default value of an ``ARG`` in the Dockerfile."""
    found = re.search(rf"^ARG {name}=(\S+)$", _dockerfile(), re.MULTILINE)
    assert found is not None, f"the Dockerfile no longer declares ARG {name}"
    return found.group(1)


def _west_revision(name: str) -> str:
    """The ``revision:`` of a project in west.yml, as it is written there."""
    manifest = (REPO_ROOT / "west.yml").read_text(encoding="utf-8")
    found = re.search(rf"name: {name}\s+remote: \S+\s+revision: (\S+)", manifest)
    assert found is not None, f"west.yml no longer states the {name} revision as expected"
    return found.group(1)


def _record_module():
    """``workspace-record.py`` as a module — its name has a hyphen in it."""
    spec = importlib.util.spec_from_file_location("workspace_record", RECORD_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------
# The revisions the Dockerfile restates
# --------------------------------------------------------------------------


def test_the_dockerfile_fetches_the_zephyr_tag_west_yml_pins() -> None:
    """The reason the tag is fetched at all is that it decides firmware bytes.

    A shallow clone cannot see its tag, so ``git describe`` answers with
    the commit, Zephyr stamps that into ``BUILD_VERSION`` and the boot
    banner, and the image differs from one built in a full checkout. The
    Dockerfile fetches the tag ref back — for the *stated* revision. If
    that one and ``west.yml`` disagree, the fetch either fails or, worse,
    succeeds for a tag that is not the checked-out commit.
    """
    assert _arg("ZEPHYR_REVISION") == _west_revision("zephyr")


def test_the_dockerfile_fetches_the_chip_tag_west_yml_pins() -> None:
    assert _arg("CHIP_REVISION") == _west_revision("connectedhomeip")


def test_only_those_two_projects_are_pinned_by_tag() -> None:
    """The tag fetch covers ``west.yml``'s tag pins, and it has to keep doing so.

    Everything else in the workspace comes from Zephyr's own manifest,
    pinned by SHA, where ``git describe --always`` answers with that SHA
    either way. A third tag-pinned project added to ``west.yml`` needs a
    third fetch in the Dockerfile, and this is where that gets noticed.
    """
    manifest = (REPO_ROOT / "west.yml").read_text(encoding="utf-8")
    revisions = re.findall(r"^\s+revision: (\S+)$", manifest, re.MULTILINE)
    tags = [revision for revision in revisions if not re.fullmatch(r"[0-9a-f]{40}", revision)]
    assert sorted(tags) == sorted([_arg("ZEPHYR_REVISION"), _arg("CHIP_REVISION")])


# --------------------------------------------------------------------------
# The patch set the image applies
# --------------------------------------------------------------------------


def _expanded_patch_names() -> list[str]:
    """The patch file names the Dockerfile builds out of its two ARGs."""
    text = _dockerfile()
    names = re.findall(r"/tmp/patches/(\S+?\.patch)", text)
    expanded = set()
    for name in names:
        expanded.add(
            name.replace("${ZEPHYR_REVISION}", _arg("ZEPHYR_REVISION")).replace(
                "${CHIP_REVISION}", _arg("CHIP_REVISION")
            )
        )
    return sorted(expanded)


def test_the_patches_the_image_applies_are_the_patches_in_the_repository() -> None:
    """Both directions, because both have bitten somebody.

    A patch renamed without the Dockerfile following it makes the image
    build fail — loudly, which is fine. A patch *added* to ``patches/``
    without a line here does not fail anything: the image is simply built
    without it, and the difference shows up as a compile error or, worse,
    as behaviour, a quarter of an hour into a Matter build.
    """
    on_disk = sorted(path.name for path in (REPO_ROOT / "patches").glob("*.patch"))
    assert _expanded_patch_names() == on_disk


def test_the_patches_are_applied_with_plain_git_apply() -> None:
    """No ``--3way``, no ``--reject``, no retry (patches/README.md, ci.yml).

    A patch that has drifted from the revision ``west.yml`` pins must fail
    the image build. Every fallback ``git apply`` offers turns that
    failure into a tree nobody described.
    """
    applies = re.findall(r"git -C \S+ apply(.*)", _dockerfile())
    assert applies, "the Dockerfile no longer applies the patch set"
    for options in applies:
        assert "--3way" not in options
        assert "--reject" not in options


# --------------------------------------------------------------------------
# The build context, which is now the repository
# --------------------------------------------------------------------------


def test_everything_the_dockerfile_copies_is_in_the_context() -> None:
    """The context widened from ``containers/builder/`` to the repository.

    ``west.yml`` and ``patches/`` are image inputs now and live outside
    that directory. Every ``COPY`` source is therefore a repository-root
    relative path, and a file moved inside the repository breaks the image
    build — which is a thing to find out here rather than in CI's image
    job ten minutes later.
    """
    for line in _dockerfile().splitlines():
        if not line.startswith("COPY ") or "--from=" in line:
            continue
        for source in line.split()[1:-1]:
            assert (REPO_ROOT / source).exists(), f"{source} is not in the build context"


def test_the_context_excludes_what_it_should_and_keeps_what_it_must() -> None:
    """A ``.dockerignore`` that swallows an image input is a broken build."""
    ignored = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8")
    patterns = [line.strip() for line in ignored.splitlines() if line.strip()[:1] not in ("#", "")]
    assert ".git" in patterns
    assert "build/" in patterns
    for kept in ("west.yml", "patches", "containers"):
        assert kept not in patterns


# --------------------------------------------------------------------------
# Where the workspace is, and that the image can read it
# --------------------------------------------------------------------------


def test_the_manifest_directory_is_where_west_yml_says_the_sdk_goes() -> None:
    """``self: path:`` decides where west looks for the manifest repository.

    The Dockerfile copies ``west.yml`` there to run ``west init -l``, and
    empties the directory again so the SDK can be mounted into it. Both
    have to be the path ``west.yml`` names, because west recomputes it
    from ``.west/config`` on every CMake configure.
    """
    manifest = (REPO_ROOT / "west.yml").read_text(encoding="utf-8")
    found = re.search(r"^\s+self:\s*\n\s+path: (\S+)$", manifest, re.MULTILINE)
    assert found is not None, "west.yml no longer states self: path:"
    sdk_dir = f"{TOPDIR}/{found.group(1)}"

    text = _dockerfile()
    assert f"COPY west.yml {sdk_dir}/west.yml" in text
    assert f"rm -rf {sdk_dir} && mkdir {sdk_dir}" in text


def test_the_workspace_is_readable_by_a_uid_that_does_not_own_it() -> None:
    """The one that fails silently, so the one worth pinning.

    The workspace is baked as root and the container runs as the calling
    user's UID, which git refuses to work with (CVE-2022-24765 hardening).
    West's ``git ls-tree manifest-rev`` then fails loudly — but Zephyr's
    ``git describe`` fails by leaving ``BUILD_VERSION`` unset, with no
    error anywhere and a different firmware at the end.
    """
    assert f"git config --system --add safe.directory '{TOPDIR}/*'" in _dockerfile()


def test_the_record_is_outside_every_layer() -> None:
    """Inside a layer it would show up in ``git status``; at the topdir a
    writable view mounted over the tree would shadow it. ``/mcuhome/`` is
    the namespace the build-container contract reserves for us."""
    text = _dockerfile()
    assert "--output /mcuhome/workspace.json" in text
    assert f"--output {TOPDIR}/" not in text


def test_the_image_claims_no_contract_conformance() -> None:
    """``org.mcuhome.contract=1`` promises ``/mcuhome/run`` exists (§2.2).

    It does not exist yet. A label without the program behind it is a
    false claim to exactly the third parties the contract is written for,
    so the labels arrive with the program and not before.
    """
    assert "org.mcuhome." not in _dockerfile()


# --------------------------------------------------------------------------
# What the record says
# --------------------------------------------------------------------------


def test_the_recorded_layer_names_are_the_contract_registry() -> None:
    """Layer names are an append-only registry the contract owns (§1.1).

    They are what a later ``describe`` fills its ``trees`` block from, so
    a private spelling here would mean a translation step nobody wrote.
    """
    module = _record_module()
    contract = (REPO_ROOT / "docs" / "design" / "build-container-contract.md").read_text(
        encoding="utf-8"
    )
    found = re.search(r"defines the layer names\s*\n?\s*(`[^\n]+`)", contract)
    assert found is not None, "the contract no longer states its layer registry as expected"
    registry = set(re.findall(r"`(\w+)`", found.group(1)))
    assert set(module.LAYER_PROJECTS) | {module.SDK_LAYER} == registry


def test_the_record_names_the_resolved_commit_and_the_patch_digest(tmp_path, monkeypatch) -> None:
    """A digest says "the same"; it never says "what".

    Two revisions in ``west.yml`` are movable tags, so a rebuild of the
    same Dockerfile can produce a different workspace. The 40-character
    commit per layer is what makes that checkable. Patches are identified
    by SHA-256 because ``patches/README.md`` prescribes regenerating one
    in place under its own name — the name is not an identity.
    """
    module = _record_module()
    topdir = tmp_path / "workspace"
    (topdir / ".west").mkdir(parents=True)
    config = topdir / ".west" / "config"
    config.write_text("[manifest]\npath = mcuhome\nfile = west.yml\n", "utf-8")
    patch = tmp_path / "zephyr-v4.4.0-nrf53-spinel-stack.patch"
    patch.write_bytes(b"--- a\n+++ b\n")

    monkeypatch.setattr(
        module,
        "_west_projects",
        lambda _topdir: {
            "zephyr": {"path": str(topdir / "zephyr"), "revision": "v4.4.0"},
            "connectedhomeip": {
                "path": str(topdir / "modules/lib/connectedhomeip"),
                "revision": "v1.5.1.0",
            },
            "mcuboot": {"path": str(topdir / "bootloader/mcuboot"), "revision": "a" * 40},
        },
    )
    monkeypatch.setattr(module, "_git", lambda _repository, *_arguments: "b" * 40)

    record = module.build_record(topdir, "narrow-depth-1", [("zephyr", patch)])
    # Serialisable, because a JSON document is what the image carries.
    assert json.loads(json.dumps(record)) == record

    assert record["topdir"] == str(topdir)
    assert record["manifest"] == {"path": "mcuhome", "file": "west.yml"}
    assert record["layers"]["zephyr"]["commit"] == "b" * 40
    assert record["layers"]["zephyr"]["revision"] == "v4.4.0"
    assert record["layers"]["zephyr"]["patches"] == [
        {
            "file": "zephyr-v4.4.0-nrf53-spinel-stack.patch",
            # sha256 of the two lines written above, not of the name.
            "sha256": "6e53bf2ad8f234c60294a05a013874a211fe3c03653f48df43ac4ec085ab9a24",
        }
    ]
    # A layer nobody patched says so, rather than saying nothing.
    assert record["layers"]["mcuboot"]["patches"] == []


def test_the_sdk_layer_is_recorded_as_absent(tmp_path, monkeypatch) -> None:
    """ADR 0018 makes it a hash-pinned package fetched per build.

    So it is a tree the image knows the location of and not the content
    of — which the contract already models as a ``trees`` entry without a
    version. Recording it as a normal layer would be a claim about bytes
    the image does not have.
    """
    module = _record_module()
    topdir = tmp_path / "workspace"
    (topdir / ".west").mkdir(parents=True)
    (topdir / ".west" / "config").write_text("[manifest]\npath = mcuhome\n", "utf-8")
    monkeypatch.setattr(
        module,
        "_west_projects",
        lambda _topdir: {
            project: {"path": str(topdir / project), "revision": "x"}
            for project in module.LAYER_PROJECTS.values()
        },
    )
    monkeypatch.setattr(module, "_git", lambda _repository, *_arguments: "c" * 40)

    layer = module.build_record(topdir, "narrow-depth-1", [])["layers"][module.SDK_LAYER]
    assert layer == {"path": str(topdir / "mcuhome"), "mounted": True}
    assert "revision" not in layer


def test_a_layer_west_does_not_know_is_a_failure_not_an_omission(tmp_path, monkeypatch) -> None:
    """A silently missing layer is an image that lies about itself."""
    module = _record_module()
    topdir = tmp_path / "workspace"
    (topdir / ".west").mkdir(parents=True)
    (topdir / ".west" / "config").write_text("[manifest]\npath = mcuhome\n", "utf-8")
    monkeypatch.setattr(module, "_west_projects", lambda _topdir: {"some-other-project": {}})

    with pytest.raises(SystemExit):
        module.build_record(topdir, "narrow-depth-1", [])
