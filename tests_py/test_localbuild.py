# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Driving a ``local`` build from a device model (``localbuild.py``).

**Docker never runs here** — the same rule as ``test_localbackend.py``, and
this module reuses that suite's scripted seam. What is asserted is the
composition above the backend: that a device model becomes a locked
context and one ``build`` invocation, that the two typed refusals E54 asks
for (a missing image, a missing SDK source) land before a container
starts, and — the E55 security invariant — that the **private** key never
appears in any docker argv and the context carries only the public half.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import EXAMPLES_DIR, resolve_file
from test_localbackend import (
    IMAGE,
    Seam,
    build_result,
    describe_result_document,
    image_facts,
    make_sdk_source,
)

from mcuhome.compiler import localbuild
from mcuhome.compiler.localbackend import Docker
from mcuhome.model.errors import BuildError
from mcuhome.workbench.contextdir import read_context_manifest
from mcuhome.workbench.signing import (
    generate_key_pem,
    looks_like_p256_key,
    looks_like_p256_public_key,
    public_key_pem,
)

#: A P-256 key with a known scalar, so this module never draws one.
TEST_SCALAR = 0x00112233445566778899AABBCCDDEEFF00112233445566778899AABBCCDDEEF0


@pytest.fixture
def model():
    return resolve_file(EXAMPLES_DIR / "00-bmp180-two-endpoints.yaml")


@pytest.fixture
def public_pem() -> str:
    return public_key_pem(generate_key_pem(TEST_SCALAR))


def _conforming(request) -> None:
    """Play a conforming build, computing the context id the backend expects.

    The context id is not known until :func:`run_local_build` has created
    and locked the context, so the seam reads it back from the manifest at
    the ``context`` path the request names — exactly what a real program
    does when it computes ``result.context`` from the context as mounted.
    """
    manifest = read_context_manifest(Path(request["context"]) / "manifest.yaml")
    build_result(request, context=manifest.compute_id())


def _seam(**overrides) -> Seam:
    return Seam(
        facts=overrides.pop("facts", image_facts()),
        build=overrides.pop("build", _conforming),
        describe_static=overrides.pop("describe_static", describe_result_document()),
        **overrides,
    )


def _flatten(calls: list[list[str]]) -> str:
    return "\n".join(" ".join(argv) for argv in calls)


# --------------------------------------------------------------------------
# The happy path: model -> context -> one build invocation
# --------------------------------------------------------------------------


def test_run_local_build_composes_a_context_and_drives_one_build(tmp_path, model, public_pem):
    make_sdk_source(tmp_path / "src")
    seam = _seam()
    result = localbuild.run_local_build(
        model,
        signing_pub=public_pem,
        sdk_sources=(tmp_path / "src",),
        work_root=tmp_path / "wr",
        env={},
        image=IMAGE,
        jobs=2,
        docker=Docker(runner=seam),
    )
    assert result.outcome.successful, result.outcome.problems
    assert result.image == IMAGE
    # A locked context was created from the model, with the pins the pin
    # resolution produced.
    manifest = read_context_manifest(result.context_dir / "manifest.yaml")
    assert manifest.board == model.device.board
    # The delivered artifacts are where the result says they are.
    assert (result.out_dir / "firmware.bin").is_file()
    assert (result.out_dir / "build-report.json").is_file()
    assert {a.role for a in result.outcome.artifacts} == {"firmware", "report"}


# --------------------------------------------------------------------------
# E55: the private key is never passed, never mounted, never in an argv
# --------------------------------------------------------------------------


def test_the_private_key_never_appears_in_any_docker_argv(tmp_path, model):
    """The container gets keys/signing.pub and nothing else of the key pair.

    A private key file exists on this host, and its bytes and its path are
    grepped for across every composed docker command the backend produced —
    the run that starts the container, the exec that invokes the program,
    every mount argument. It appears in none of them, because
    :func:`run_local_build` has no way to receive it: its only key input is
    the public PEM.
    """
    private_pem = generate_key_pem(TEST_SCALAR)
    private_path = tmp_path / "signing.key"
    private_path.write_text(private_pem, encoding="utf-8")
    public_pem = public_key_pem(private_pem)

    make_sdk_source(tmp_path / "src")
    seam = _seam()
    result = localbuild.run_local_build(
        model,
        signing_pub=public_pem,
        sdk_sources=(tmp_path / "src",),
        work_root=tmp_path / "wr",
        env={},
        image=IMAGE,
        docker=Docker(runner=seam),
    )
    assert result.outcome.successful

    # The container really was started and the program really was invoked —
    # so the grep below is over a real invocation, not an empty one.
    assert any("--detach" in argv for argv in seam.calls)
    assert any(argv[1] == "exec" for argv in seam.calls)

    flat = _flatten(seam.calls)
    assert str(private_path) not in flat
    assert "PRIVATE KEY" not in flat  # no private key material rode along in an argv
    # What the context does carry is the public half, and only that.
    signing_pub = (result.context_dir / "keys" / "signing.pub").read_text(encoding="utf-8")
    assert looks_like_p256_public_key(signing_pub)
    assert not looks_like_p256_key(signing_pub)
    # And the context is mounted read-only, so even the public key cannot
    # be written back by the container.
    start = next(argv for argv in seam.calls if "--detach" in argv)
    mounts = [start[i + 1] for i, item in enumerate(start) if item == "--volume"]
    assert f"{result.context_dir}:{result.context_dir}:ro" in mounts


# --------------------------------------------------------------------------
# The two typed refusals E54 asks be surfaced cleanly (image, SDK source)
# --------------------------------------------------------------------------


def test_a_missing_image_refuses_before_a_container_starts(tmp_path, model, public_pem):
    make_sdk_source(tmp_path / "src")

    def runner(argv, on_line=None):
        if argv[1:3] == ["image", "inspect"]:
            return _missing_image()
        raise AssertionError(f"nothing else is asked once the image is missing: {argv}")

    with pytest.raises(BuildError) as caught:
        localbuild.run_local_build(
            model,
            signing_pub=public_pem,
            sdk_sources=(tmp_path / "src",),
            work_root=tmp_path / "wr",
            env={},
            image=IMAGE,
            docker=Docker(runner=runner),
        )
    assert "answers to" in caught.value.message


def test_no_sdk_source_configured_is_a_typed_refusal(tmp_path, model, public_pem):
    calls: list[list[str]] = []

    def runner(argv, on_line=None):
        calls.append(argv)
        if argv[1:3] == ["image", "inspect"]:
            return _image_ok()
        raise AssertionError(f"no container should start with no SDK: {argv}")

    with pytest.raises(BuildError) as caught:
        localbuild.run_local_build(
            model,
            signing_pub=public_pem,
            sdk_sources=(),
            work_root=tmp_path / "wr",
            env={},
            image=IMAGE,
            docker=Docker(runner=runner),
        )
    assert "SDK source" in caught.value.message
    assert not any("--detach" in argv for argv in calls)


def test_a_source_without_the_package_is_a_typed_refusal(tmp_path, model, public_pem):
    empty = tmp_path / "empty"
    empty.mkdir()

    def runner(argv, on_line=None):
        if argv[1:3] == ["image", "inspect"]:
            return _image_ok()
        raise AssertionError(f"no container should start: {argv}")

    with pytest.raises(BuildError) as caught:
        localbuild.run_local_build(
            model,
            signing_pub=public_pem,
            sdk_sources=(empty,),
            work_root=tmp_path / "wr",
            env={},
            image=IMAGE,
            docker=Docker(runner=runner),
        )
    assert localbuild.lb.SDK_PACKAGE_NAME in caught.value.message


# --------------------------------------------------------------------------
# A locally built image carries no repo digest — the placeholder path
# --------------------------------------------------------------------------


def test_a_local_image_without_a_digest_still_builds(tmp_path, model, public_pem):
    """A ``--image localhost/…`` names no pinnable bytes; the build proceeds.

    ``docker image inspect`` reports no ``RepoDigests``, so the context
    pins a placeholder digest and the backend's §9.1 cross-check tolerates
    the ``null`` observed one. The build is judged successful all the same.
    """
    make_sdk_source(tmp_path / "src")
    seam = _seam(facts=image_facts(digest=None))
    result = localbuild.run_local_build(
        model,
        signing_pub=public_pem,
        sdk_sources=(tmp_path / "src",),
        work_root=tmp_path / "wr",
        env={},
        image="localhost/builder:wip",
        docker=Docker(runner=seam),
    )
    assert result.outcome.successful, result.outcome.problems
    manifest = read_context_manifest(result.context_dir / "manifest.yaml")
    assert manifest.container.digest.startswith("sha256:")


# --------------------------------------------------------------------------
# resolve_sdk_pin in isolation
# --------------------------------------------------------------------------


def test_resolve_sdk_pin_reads_the_source_index(tmp_path):
    real = make_sdk_source(tmp_path / "src")
    constraint, version, sha256 = localbuild.resolve_sdk_pin((tmp_path / "src",))
    assert sha256 == real
    assert version
    assert constraint == localbuild.SDK_ANY


def test_resolve_sdk_pin_resolves_a_dev_only_source_under_any(tmp_path):
    """SDK_ANY means the newest, and during development that is a dev release.

    The regression this pins: the E52 pre-release rule (a dev version
    satisfies only a pre-release constraint) is right for a real pin like
    ``~=2.3`` and wrong for "any" — SDK_ANY is literally any, including a
    ``0.1.0.dev0``. An earlier version resolved SDK_ANY as a stable
    specifier and refused a dev-only source, which is exactly what every
    build did before the first stable release: the source directory holds
    one archive and it carries the ``.dev0`` version.
    """
    import json

    source = tmp_path / "src"
    source.mkdir()
    (source / "index.json").write_text(
        json.dumps(
            {
                "packages": {
                    "mcuhome-sdk": {
                        "0.1.0.dev0": {
                            "file": "mcuhome-sdk-0.1.0.dev0.tar.zst",
                            "sha256": "ab" * 32,
                            "size": 100,
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    constraint, version, sha256 = localbuild.resolve_sdk_pin((source,))
    assert version == "0.1.0.dev0"
    assert sha256 == "ab" * 32
    assert constraint == localbuild.SDK_ANY


def test_resolve_sdk_pin_without_a_source_refuses(tmp_path):
    with pytest.raises(BuildError) as caught:
        localbuild.resolve_sdk_pin(())
    assert "SDK source" in caught.value.message


def _image_ok():
    from mcuhome.compiler.localbackend import Completed

    return Completed(0, image_facts())


def _missing_image():
    from mcuhome.compiler.localbackend import Completed

    return Completed(1, "No such image")
