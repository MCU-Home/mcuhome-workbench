# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Coming back to a build directory (``buildrecord.py``).

What a build leaves behind, read by somebody who did not run it. The
builds here are real — ``test_localbuild``'s scripted container runtime,
driven through ``build_firmware`` — because a record written by a test is
the one thing that cannot establish that a build writes one.

* **The record.** A build writes ``.mcuhome-build.json`` when it ends,
  whatever the verdict, and ``read_build`` answers what the directory
  holds — from that record, or from the files for a directory nobody
  recorded, or ``None``. It re-computes no hash, which is asserted the
  only way such a promise can be: an artifact is replaced after the build
  and still appears, under the hash the build declared.
* **Cleaning.** ``clean_build`` holds the directory while it works,
  refuses while somebody else is in it, and removes what a build wrote
  and nothing else.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from conftest import EXAMPLES_DIR, ScriptedRegistry, resolve_file
from mcuhome.model.artifacts import Artifact
from test_buildlock import held_elsewhere
from test_localbuild import Seam, make_sdk_source

from mcuhome.workbench import api, build, buildrecord, containerbuild, sessionclient
from mcuhome.workbench.buildenvsession import StepResult
from mcuhome.workbench.buildlock import BUILD_LOCK_FILE, BuildDirectoryBusy
from mcuhome.workbench.imgtool import BUILD_REPORT_FILE
from mcuhome.workbench.signing import generate_key_pem, public_key_pem

#: A fixed public key, so nothing here draws one.
_PUBLIC_PEM = public_key_pem(generate_key_pem(scalar=0x1D0BEEF))


@pytest.fixture
def model():
    return resolve_file(EXAMPLES_DIR / "00-bmp180-two-endpoints.yaml")


def _options(sources: Path) -> build.BuildOptions:
    return build.BuildOptions(
        sdk_sources=(sources,), workspace_sources=(sources,), tools_sources=(sources,)
    )


def _built(tmp_path, model, monkeypatch, **overrides) -> build.BuildResult:
    """One real container build through ``build_firmware``, into ``build/``.

    The composition is the true one — it creates the context, resolves
    the environment and drives a step — and only the container runtime
    and the image registry are scripted, exactly as ``test_localbuild``
    scripts them.
    """
    make_sdk_source(tmp_path / "src")
    seam = overrides.pop("seam", None) or Seam()
    original = build.compose_container_build

    def composed(*args, **kwargs):
        kwargs["runtime"] = containerbuild.ContainerRuntime(runner=seam, spawner=seam.spawn)
        kwargs["images"] = ScriptedRegistry()
        return original(*args, **kwargs)

    monkeypatch.setattr(build, "compose_container_build", composed)
    request = build.BuildRequest(
        model=model,
        out_dir=tmp_path / "build",
        signing_pub=_PUBLIC_PEM,
        options=_options(tmp_path / "src"),
        **overrides,
    )
    return asyncio.run(build.build_firmware(request, target="local"))


def _record_document(out_dir: Path) -> dict[str, Any]:
    return json.loads((out_dir / buildrecord.BUILD_RECORD_FILE).read_text("utf-8"))


# --------------------------------------------------------------------------
# What a build leaves behind
# --------------------------------------------------------------------------


def test_a_finished_build_directory_answers_its_record(tmp_path, model, monkeypatch) -> None:
    """The whole point: a second reader learns what the builder knew.

    Nothing of the build is in this process any more — the record is read
    off the directory and has to carry the device, the identity the work
    was attributed to, the image that ran and the artifacts, or a client
    that comes back after a restart is left guessing from file names.
    """
    result = _built(tmp_path, model, monkeypatch)
    assert result.ok

    record = api.read_build(tmp_path / "build")

    assert record is not None
    assert record.device == model.device.name
    assert record.context_id == result.context_id
    assert record.container_image == result.container_image
    assert record.report == BUILD_REPORT_FILE
    assert record.artifacts == result.artifacts
    assert record.out_dir == result.out_dir
    assert (record.out_dir / record.report).is_file()
    assert record.busy is False
    assert record.signed == ()


def test_the_record_is_the_hidden_file_the_reference_names(tmp_path, model, monkeypatch) -> None:
    """A name, a place and a format version, because a reader depends on all three.

    The file is bookkeeping in a directory that belongs to the user, so it
    is hidden and prefixed like every other; its keys are the ones the
    reference states, spelled the way every document of this package
    spells keys.
    """
    _built(tmp_path, model, monkeypatch)

    path = tmp_path / "build" / ".mcuhome-build.json"
    assert path.name == buildrecord.BUILD_RECORD_FILE
    assert path.is_file()
    document = json.loads(path.read_text("utf-8"))
    assert document["build"] == buildrecord.RECORD_VERSION
    assert sorted(document) == [
        "artifacts",
        "build",
        "container_image",
        "context_id",
        "device",
        "out_dir",
        "report",
    ]
    assert all(key == key.lower() and " " not in key and "-" not in key for key in document)


def test_a_build_that_failed_records_that_there_is_nothing_here(
    tmp_path, model, monkeypatch
) -> None:
    """A verdict is not what makes a record worth writing — presence is.

    A client that comes back to a directory whose last build failed must
    learn that it holds no firmware. Writing a record only for a build
    that worked would leave yesterday's successful one in place, and the
    client would show artifacts that the failed run's fresh output
    directory no longer has.
    """

    def failed(model_, **kwargs):
        return containerbuild.ContainerBuildResult(
            outcome=StepResult(
                action="build",
                context_id="sha256:" + "f" * 64,
                exit_code=1,
                status="failure",
                problems=("the build failed",),
                out_dir=tmp_path / "out",
            ),
            out_dir=tmp_path / "out",
            context_dir=tmp_path / "context",
            container_image="ghcr.io/mcu-home/build-environment@sha256:" + "d" * 64,
        )

    monkeypatch.setattr(build, "compose_local_build", failed)
    request = build.BuildRequest(model=model, out_dir=tmp_path / "build")
    result = asyncio.run(build.build_firmware(request, target="local"))
    assert result.ok is False

    record = api.read_build(tmp_path / "build")
    assert record is not None
    assert record.artifacts == ()
    assert record.device == model.device.name
    assert record.context_id == "sha256:" + "f" * 64


def test_a_stopped_build_that_delivered_nowhere_records_the_directory_it_held(
    tmp_path, model, monkeypatch
) -> None:
    """A stopped remote build answers ``out_dir=None``, and is still a build.

    Nothing was delivered, so there is no delivery directory to record —
    and the record then names the directory it was asked to build in,
    because a client that reads it is asking about *that* directory and
    must not be handed a ``None`` to render.
    """

    async def stopped(context_dir, **kwargs):
        del context_dir, kwargs
        return sessionclient.RemoteBuildResult(
            action="build",
            context_id="",
            status="cancelled",
            artifacts=(),
            out_dir=None,
        )

    make_sdk_source(tmp_path / "src")
    monkeypatch.setattr(sessionclient, "run_remote_build", stopped)
    request = build.BuildRequest(
        model=model,
        out_dir=tmp_path / "build",
        signing_pub=_PUBLIC_PEM,
        options=_options(tmp_path / "src"),
        builder=api.SelectedBuilder(target="remote", server="10.0.0.5:8291"),
    )
    result = asyncio.run(build.build_firmware(request, target="remote"))
    assert (result.ok, result.stopped) == (False, True)
    assert result.out_dir is None

    record = api.read_build(tmp_path / "build")

    assert record is not None
    assert _record_document(tmp_path / "build")["out_dir"] is None
    assert record.out_dir == tmp_path / "build"
    assert record.device == model.device.name
    assert record.artifacts == ()
    assert record.signed == ()


def test_a_refused_build_leaves_the_record_of_the_build_that_ran(
    tmp_path, model, monkeypatch
) -> None:
    """A refusal is not a build: it changes nothing about the directory.

    The record says what is in there, and what is in there after a
    refusal is what the last build that actually ran put there.
    """
    result = _built(tmp_path, model, monkeypatch)
    before = _record_document(tmp_path / "build")

    def refuses(model_, **kwargs):
        raise api.EnvironmentUnavailable("no build environment answers for this context")

    monkeypatch.setattr(build, "compose_local_build", refuses)
    request = build.BuildRequest(model=model, out_dir=tmp_path / "build")
    with pytest.raises(api.EnvironmentUnavailable):
        asyncio.run(build.build_firmware(request, target="local"))

    assert _record_document(tmp_path / "build") == before
    record = api.read_build(tmp_path / "build")
    assert record is not None
    assert record.artifacts == result.artifacts


def test_a_record_that_cannot_be_written_does_not_cost_the_build(
    tmp_path, model, monkeypatch
) -> None:
    """The record is a shortcut, never a condition of a build succeeding.

    A directory the record cannot be written into is a directory the
    build itself wrote into — so a build that failed over the bookkeeping
    would be firmware lost to a file nobody asked for. The obstacle here
    is a directory under the record's own name, which is the cheapest
    real ``OSError`` there is.
    """
    out = tmp_path / "build"
    (out / buildrecord.BUILD_RECORD_FILE).mkdir(parents=True)

    result = _built(tmp_path, model, monkeypatch)

    assert result.ok
    assert (out / buildrecord.BUILD_RECORD_FILE).is_dir()
    assert buildrecord.write_build_record(out, result=result) is None


# --------------------------------------------------------------------------
# Reading a directory that holds no record — and one that holds nothing
# --------------------------------------------------------------------------


def test_an_empty_or_unknown_directory_answers_none(tmp_path) -> None:
    """Three ways of holding no build, one answer, and no exception.

    ``read_build`` is how a client asks the question at all, so every
    "there is nothing here" has to be an answer: a path that does not
    exist, an empty directory, and a directory somebody keeps notes in.
    """
    assert api.read_build(tmp_path / "never-built") is None

    empty = tmp_path / "empty"
    empty.mkdir()
    assert api.read_build(empty) is None

    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "shopping-list.txt").write_text("solder\n", "utf-8")
    (notes / "firmware.bin.bak").write_bytes(b"not an artifact")
    assert api.read_build(notes) is None


def test_a_directory_no_build_recorded_is_read_off_its_files(tmp_path) -> None:
    """A build from an older version, or one somebody copied over.

    The files are the fallback and they carry less: the report and the
    firmware are there to be found, and what only a record knows stays
    empty rather than being guessed from the directory's name — a guess
    in a record reads exactly like a fact.
    """
    out = tmp_path / "bmp180-node"
    out.mkdir()
    (out / BUILD_REPORT_FILE).write_text('{"report": 1}', "utf-8")
    (out / "firmware.bin").write_bytes(b"BIN")
    (out / "firmware.hex").write_text(":00000001FF\n", "utf-8")
    (out / "firmware.signed.bin").write_bytes(b"SIGNED")

    record = api.read_build(out)

    assert record is not None
    assert record.out_dir == out
    assert record.device == ""
    assert record.context_id == ""
    assert record.container_image == ""
    assert record.report == BUILD_REPORT_FILE
    assert [(artifact.path, artifact.role) for artifact in record.artifacts] == [
        (BUILD_REPORT_FILE, "report"),
        ("firmware.bin", "firmware"),
        ("firmware.hex", "firmware"),
    ]
    # Nothing measured these, and a hash computed now would state the
    # current content as the built one.
    assert {artifact.sha256 for artifact in record.artifacts} == {""}
    assert [(signed.format, signed.path.name) for signed in record.signed] == [
        ("bin", "firmware.signed.bin")
    ]


@pytest.mark.parametrize(
    "written",
    ["", "{", '{"build": 2, "device": "thermostat"}', '["not", "a", "document"]'],
    ids=["empty", "truncated", "another version", "not an object"],
)
def test_a_record_this_version_cannot_read_falls_back_to_the_files(tmp_path, written) -> None:
    """Bookkeeping that cannot be read is not a client's problem.

    The record belongs to this package; the question the client asked is
    about the *build*. So an unreadable one costs the answer its detail
    and never its correctness — the files are still there.
    """
    out = tmp_path / "build"
    out.mkdir()
    (out / buildrecord.BUILD_RECORD_FILE).write_text(written, "utf-8")
    (out / BUILD_REPORT_FILE).write_text('{"report": 1}', "utf-8")
    (out / "firmware.bin").write_bytes(b"BIN")

    record = api.read_build(out)

    assert record is not None
    assert record.device == ""
    assert [artifact.path for artifact in record.artifacts] == [BUILD_REPORT_FILE, "firmware.bin"]


def test_reading_verifies_no_hash_so_a_replaced_artifact_still_appears(
    tmp_path, model, monkeypatch
) -> None:
    """The contract must not be quietly stronger than the document.

    The reference promises that ``read_build`` re-verifies nothing, and a
    client reading a build directory of a hundred megabytes depends on
    that promise being kept. So an artifact is replaced after the build
    and the record still lists it, under the hash the build declared —
    which is the *only* way to tell "verifies nothing" from "happens not
    to have been tampered with today".
    """
    result = _built(tmp_path, model, monkeypatch)
    firmware = result.out_dir / "firmware.bin"
    declared = next(
        artifact.sha256 for artifact in result.artifacts if artifact.path == "firmware.bin"
    )
    firmware.write_bytes(b"somebody else's firmware")

    record = api.read_build(tmp_path / "build")

    assert record is not None
    assert [artifact.path for artifact in record.artifacts] == [
        artifact.path for artifact in result.artifacts
    ]
    replaced = next(
        artifact.sha256 for artifact in record.artifacts if artifact.path == "firmware.bin"
    )
    assert replaced == declared, "the declared hash is answered, not a fresh measurement"


def test_an_artifact_that_is_gone_is_still_what_the_build_declared(
    tmp_path, model, monkeypatch
) -> None:
    """Deleting a file does not rewrite history either.

    Same promise from the other side: the record states what the build
    delivered, so a client that wants to know whether the file is still
    there looks, rather than expecting this call to have looked.
    """
    result = _built(tmp_path, model, monkeypatch)
    (result.out_dir / "firmware.bin").unlink()

    record = api.read_build(tmp_path / "build")

    assert record is not None
    assert "firmware.bin" in [artifact.path for artifact in record.artifacts]


def test_a_signed_build_answers_the_signed_images_beside_the_unsigned(
    tmp_path, model, monkeypatch
) -> None:
    """Signing happens after the build, so only the directory can say.

    The record cannot carry them — it was written before the signature
    existed — which is why ``signed`` is read off the delivery directory
    every time.
    """
    result = _built(tmp_path, model, monkeypatch)
    (result.out_dir / "firmware.signed.bin").write_bytes(b"SIGNED BIN")
    (result.out_dir / "firmware.signed.hex").write_text(":00000001FF\n", "utf-8")

    record = api.read_build(tmp_path / "build")

    assert record is not None
    assert [(signed.format, signed.path) for signed in record.signed] == [
        ("bin", result.out_dir / "firmware.signed.bin"),
        ("hex", result.out_dir / "firmware.signed.hex"),
    ]


def test_a_directory_somebody_is_working_in_says_so(tmp_path, model, monkeypatch) -> None:
    """``busy`` is the difference between "this is the build" and "this is a build in flight".

    A client that renders a record of a directory a build is writing into
    has to be able to say so, and it must get the record anyway: the
    question "what is in here" has an answer while somebody works in
    there.
    """
    _built(tmp_path, model, monkeypatch)
    assert api.read_build(tmp_path / "build").busy is False

    with held_elsewhere(tmp_path / "build", device=model.device.name):
        record = api.read_build(tmp_path / "build")

    assert record is not None
    assert record.busy is True
    assert record.to_dict()["busy"] is True


# --------------------------------------------------------------------------
# Cleaning
# --------------------------------------------------------------------------


def test_clean_build_removes_what_a_build_wrote(tmp_path, model, monkeypatch) -> None:
    """Everything a build put there goes, and the directory stays.

    The work root is the bulk of it — a build unpacks an SDK and a build
    environment in there — so a clean that removed only the artifacts
    would free a few megabytes out of a few hundred.
    """
    result = _built(tmp_path, model, monkeypatch)
    out = tmp_path / "build"
    assert (out / ".mcuhome-local").is_dir()

    removed = api.clean_build(out, device=model.device.name)

    assert out.is_dir(), "the build directory itself is not the build's to remove"
    assert not (out / ".mcuhome-local").exists()
    assert not (out / buildrecord.BUILD_RECORD_FILE).exists()
    assert not result.out_dir.exists()
    assert out / ".mcuhome-local" in removed
    assert out / buildrecord.BUILD_RECORD_FILE in removed
    assert api.read_build(out) is None


def test_clean_build_leaves_the_files_that_are_not_a_builds(tmp_path, model, monkeypatch) -> None:
    """The one thing a clean must never do is delete somebody's work.

    A build directory is a directory in a user's project: a note, a
    keepsake copy of a firmware, a log somebody is reading. None of them
    is a build's leftover, and a clean that removed them would be
    unusable exactly once.
    """
    _built(tmp_path, model, monkeypatch)
    out = tmp_path / "build"
    (out / "notes.txt").write_text("this is the one that worked\n", "utf-8")
    (out / "firmware.bin.keep").write_bytes(b"a copy somebody made")
    (out / "logs").mkdir()
    (out / "logs" / "yesterday.log").write_text("...\n", "utf-8")

    removed = api.clean_build(out)

    assert (out / "notes.txt").is_file()
    assert (out / "firmware.bin.keep").is_file()
    assert (out / "logs" / "yesterday.log").is_file()
    assert not [path for path in removed if path.name in {"notes.txt", "firmware.bin.keep", "logs"}]
    # The lock file is the guard this call is holding, not a leftover.
    assert (out / BUILD_LOCK_FILE).is_file()


def test_clean_build_removes_the_artifacts_a_client_copied_up(tmp_path) -> None:
    """A build directory as a person sees it: firmware at its top.

    The report and the firmware beside it are a build's output wherever
    they lie, so a directory whose artifacts were put at its top — by a
    client, or by a build that delivered there — is cleaned as one.
    """
    out = tmp_path / "build"
    out.mkdir()
    (out / BUILD_REPORT_FILE).write_text('{"report": 1}', "utf-8")
    (out / "firmware.bin").write_bytes(b"BIN")
    (out / "firmware.signed.bin").write_bytes(b"SIGNED")
    (out / "firmware.hex").write_text(":00000001FF\n", "utf-8")

    removed = api.clean_build(out)

    assert sorted(path.name for path in removed) == [
        BUILD_REPORT_FILE,
        "firmware.bin",
        "firmware.hex",
        "firmware.signed.bin",
    ]
    assert sorted(path.name for path in out.iterdir()) == [BUILD_LOCK_FILE]


def test_clean_build_refuses_while_a_build_is_running(tmp_path, model, monkeypatch) -> None:
    """The refusal this call exists to be able to make.

    Removing a running build's output is the collision the build lock was
    written for — the directory would be emptied under a compiler that is
    writing into it — so cleaning takes the directory like every other
    operation and says who is in the way.
    """
    _built(tmp_path, model, monkeypatch)
    out = tmp_path / "build"

    with held_elsewhere(out, device=model.device.name, operation="build"):
        with pytest.raises(BuildDirectoryBusy) as caught:
            api.clean_build(out, device=model.device.name)
        # Nothing was removed on the way to the refusal.
        assert (out / ".mcuhome-local").is_dir()
        assert (out / buildrecord.BUILD_RECORD_FILE).is_file()

    assert "already running" in str(caught.value)
    assert model.device.name in str(caught.value)


def test_a_clean_is_itself_refused_while_it_runs(tmp_path, model) -> None:
    """The word the lock file carries, so the next refusal reads right.

    ``clean`` is one of the operations a build directory can be held for,
    and a person who meets the refusal is told that the output is being
    deleted rather than that something unnamed is going on.
    """
    out = tmp_path / "build"
    out.mkdir(parents=True)
    with (
        held_elsewhere(out, device=model.device.name, operation="clean"),
        pytest.raises(BuildDirectoryBusy) as caught,
    ):
        api.clean_build(out, device=model.device.name)

    assert "being deleted" in str(caught.value)


def test_cleaning_a_directory_that_is_not_there_creates_nothing(tmp_path) -> None:
    """A clean that leaves a new empty directory behind has done the opposite."""
    missing = tmp_path / "never-built"

    assert api.clean_build(missing) == ()
    assert not missing.exists()


def test_clean_build_never_follows_a_record_out_of_the_directory(tmp_path) -> None:
    """A record is a file, and a file can be edited.

    It names paths, and this call deletes what it names — so a record
    pointing outside the build directory has to be answered with "not
    mine to remove" rather than with a deletion somewhere else on the
    disk.
    """
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "firmware.bin").write_bytes(b"somebody else's file")
    keepsake = tmp_path / "keepsake.txt"
    keepsake.write_text("mine\n", "utf-8")

    out = tmp_path / "build"
    out.mkdir()
    (out / buildrecord.BUILD_RECORD_FILE).write_text(
        json.dumps(
            {
                "build": buildrecord.RECORD_VERSION,
                "device": "bmp180-node",
                "context_id": "",
                "out_dir": str(elsewhere),
                "report": BUILD_REPORT_FILE,
                "container_image": "",
                "artifacts": [
                    {
                        "root": "out",
                        "path": "../keepsake.txt",
                        "role": "firmware",
                        "sha256": "a" * 64,
                    }
                ],
            }
        ),
        "utf-8",
    )

    removed = api.clean_build(out)

    assert (elsewhere / "firmware.bin").is_file()
    assert keepsake.is_file()
    assert [path.name for path in removed] == [buildrecord.BUILD_RECORD_FILE]


def test_the_artifact_type_is_what_the_record_carries() -> None:
    """The record answers the artifacts the build declared, in its own type."""
    record = api.BuildRecord(
        out_dir=Path("/projects/attic/build/thermostat"),
        device="thermostat",
        context_id="sha256:" + "c" * 64,
        artifacts=(Artifact(root="out", path="firmware.bin", role="firmware", sha256="a" * 64),),
        report=BUILD_REPORT_FILE,
        signed=(),
        container_image="",
        busy=False,
    )

    assert record.to_dict()["artifacts"] == [
        {"root": "out", "path": "firmware.bin", "role": "firmware", "sha256": "a" * 64}
    ]
