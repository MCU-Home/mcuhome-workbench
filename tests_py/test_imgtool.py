# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Detached signing, and the proof that it is the same signature.

The claim of ADR 0015 decision 8 is that ``mcuhome sign`` produces what
the build would have produced, so that a private key never has to be on
the machine that compiles. This module holds the evidence:

* the command is Zephyr's own, argument for argument
  (:func:`test_the_command_is_the_one_zephyr_assembles`);
* signing detached yields an image that is byte-identical to an inline
  one everywhere MCUboot looks — header, payload, protected TLVs and the
  SHA-256 over all of it — and differs **only** in the ECDSA signature,
  which cannot be identical because ECDSA draws a fresh random nonce per
  signature (:func:`test_two_signatures_of_the_same_image_differ_only_there`).

That last point is worth stating rather than working around: signing the
same bytes twice with the same key gives two different, equally valid
signatures, occasionally of different DER length. "Byte-identical image,
different signature" is therefore the strongest equivalence that exists,
and it is exactly the one a verifier cares about.
"""

from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
from pathlib import Path

import pytest
from mcuhome.compiler import workspace
from mcuhome.model.errors import BuildError
from mcuhome.model.manifest import MANIFEST_FILE, SigningParameters
from mcuhome.model.registry import SIGNATURE_TYPE

from mcuhome.workbench import imgtool, signing

PARAMETERS = SigningParameters(header_size=512, align=4, slot_size=933888, version="0.0.0+0")

#: A P-256 key with a known scalar, so the suite never draws one and
#: never touches the developer's own (see conftest's autouse fixture).
TEST_SCALAR = 0x00112233445566778899AABBCCDDEEFF00112233445566778899AABBCCDDEEF0


# --------------------------------------------------------------------------
# Finding imgtool
# --------------------------------------------------------------------------


def test_the_workspaces_own_mcuboot_wins(tmp_path) -> None:
    """The same script the inline build used, so both are one tool version."""
    script = tmp_path / imgtool.MCUBOOT_IMGTOOL
    script.parent.mkdir(parents=True)
    script.write_text("", "utf-8")
    assert imgtool.find_imgtool(env={}, topdir=tmp_path) == [sys.executable, str(script)]


def test_the_environment_variable_beats_everything(tmp_path) -> None:
    script = tmp_path / imgtool.MCUBOOT_IMGTOOL
    script.parent.mkdir(parents=True)
    script.write_text("", "utf-8")
    other = tmp_path / "other-imgtool.py"
    other.write_text("", "utf-8")
    found = imgtool.find_imgtool(env={imgtool.IMGTOOL_VAR: str(other)}, topdir=tmp_path)
    assert found == [sys.executable, str(other)]


def test_a_program_name_is_taken_as_a_program() -> None:
    assert imgtool.find_imgtool(env={imgtool.IMGTOOL_VAR: "imgtool"}) == ["imgtool"]


def test_no_imgtool_anywhere_is_a_refusal_that_says_where_to_get_one(tmp_path) -> None:
    with pytest.raises(BuildError) as caught:
        imgtool.require_imgtool(env={"PATH": str(tmp_path)}, topdir=tmp_path)
    assert "pip install imgtool" in caught.value.hint
    assert imgtool.IMGTOOL_VAR in caught.value.hint


# --------------------------------------------------------------------------
# The command
# --------------------------------------------------------------------------


def test_the_command_is_the_one_zephyr_assembles() -> None:
    """Verbatim from zephyr/cmake/mcuboot.cmake, in its order.

    Zephyr builds ``--version --header-size --slot-size``, then prepends
    ``--align`` to the argument list that already carries ``--key``, then
    the two file names. Reading this list next to the one in a build log
    has to be enough to see that they are the same call.
    """
    command = imgtool.sign_command(
        ["python", "imgtool.py"],
        parameters=PARAMETERS,
        key=Path("/keys/signing.key"),
        source=Path("/build/app/zephyr/zephyr.bin"),
        output=Path("/build/app/zephyr/zephyr.signed.bin"),
    )
    assert command == [
        "python",
        "imgtool.py",
        "sign",
        "--version",
        "0.0.0+0",
        "--header-size",
        "512",
        "--slot-size",
        "933888",
        "--align",
        "4",
        "--key",
        "/keys/signing.key",
        "/build/app/zephyr/zephyr.bin",
        "/build/app/zephyr/zephyr.signed.bin",
    ]


# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------


MANIFEST = {
    "manifest_version": 1,
    "images": [{"name": "app", "role": "application", "flash_bytes": 16, "files": []}],
    "signing": {
        "tool": "imgtool",
        "image": "app",
        "signed": False,
        "signed_by_the_build": False,
        "signature_type": "ecdsa-p256",
        "arguments": PARAMETERS.to_dict(),
        "inputs": {"bin": "build/app/zephyr/zephyr.bin"},
        "outputs": {"bin": "build/app/zephyr/zephyr.signed.bin"},
    },
}


def _build_dir(tmp_path: Path, manifest: dict | None = None) -> Path:
    import json

    output = tmp_path / "build" / "app" / "zephyr"
    output.mkdir(parents=True)
    (output / "zephyr.bin").write_bytes(bytes(512) + b"payload")
    (tmp_path / MANIFEST_FILE).write_text(json.dumps(manifest or MANIFEST), "utf-8")
    return tmp_path


def _key(tmp_path: Path) -> Path:
    path = tmp_path / "signing.key"
    path.write_text(signing.generate_key_pem(TEST_SCALAR), "utf-8")
    path.chmod(0o600)
    return path


def test_a_build_directory_and_its_manifest_both_work(tmp_path) -> None:
    out_dir = _build_dir(tmp_path)
    key = _key(tmp_path)
    env = {imgtool.IMGTOOL_VAR: "imgtool"}
    assert imgtool.plan_signing(out_dir, key=key, env=env).manifest_path == out_dir / MANIFEST_FILE
    assert imgtool.plan_signing(out_dir / MANIFEST_FILE, key=key, env=env).out_dir == out_dir


def test_the_plan_carries_the_manifests_own_parameters(tmp_path) -> None:
    plan = imgtool.plan_signing(
        _build_dir(tmp_path), key=_key(tmp_path), env={imgtool.IMGTOOL_VAR: "imgtool"}
    )
    assert plan.parameters == PARAMETERS
    assert plan.outputs == [tmp_path / "build" / "app" / "zephyr" / "zephyr.signed.bin"]
    assert plan.commands[0][1][:2] == ("imgtool", "sign")


def test_a_missing_image_is_a_refusal(tmp_path) -> None:
    out_dir = _build_dir(tmp_path)
    (out_dir / "build" / "app" / "zephyr" / "zephyr.bin").unlink()
    with pytest.raises(BuildError) as caught:
        imgtool.plan_signing(out_dir, key=_key(tmp_path), env={imgtool.IMGTOOL_VAR: "imgtool"})
    assert "not there" in caught.value.message


def test_a_manifest_without_a_signing_block_is_a_refusal(tmp_path) -> None:
    out_dir = _build_dir(tmp_path, manifest={"manifest_version": 1, "images": []})
    with pytest.raises(BuildError) as caught:
        imgtool.plan_signing(out_dir, key=_key(tmp_path), env={imgtool.IMGTOOL_VAR: "imgtool"})
    assert "'signing'" in caught.value.message


def test_plan_signing_refuses_an_absolute_input(tmp_path) -> None:
    """An absolute manifest input would read and sign a file outside the build.

    ``out_dir / "/etc/passwd"`` is ``/etc/passwd``: ``Path`` join lets an
    absolute right-hand side win. A crafted build-manifest.json handed to
    ``mcuhome sign`` must not be able to name an arbitrary file to sign, so
    the value is refused by name before it is joined.
    """
    manifest = json.loads(json.dumps(MANIFEST))
    manifest["signing"]["inputs"] = {"bin": "/etc/passwd"}
    out_dir = _build_dir(tmp_path, manifest=manifest)
    with pytest.raises(BuildError) as caught:
        imgtool.plan_signing(out_dir, key=_key(tmp_path), env={imgtool.IMGTOOL_VAR: "imgtool"})
    assert "/etc/passwd" in caught.value.message
    assert "signing input" in caught.value.message


def test_plan_signing_refuses_an_output_that_escapes_the_build_dir(tmp_path) -> None:
    """A ``..`` manifest output would write the signed image outside the build."""
    manifest = json.loads(json.dumps(MANIFEST))
    manifest["signing"]["outputs"] = {"bin": "../../evil.signed.bin"}
    out_dir = _build_dir(tmp_path, manifest=manifest)
    with pytest.raises(BuildError) as caught:
        imgtool.plan_signing(out_dir, key=_key(tmp_path), env={imgtool.IMGTOOL_VAR: "imgtool"})
    assert "evil.signed.bin" in caught.value.message
    assert "signing output" in caught.value.message
    assert not (tmp_path.parent / "evil.signed.bin").exists()


def test_imgtool_failure_carries_imgtools_own_words(tmp_path) -> None:
    plan = imgtool.plan_signing(
        _build_dir(tmp_path), key=_key(tmp_path), env={imgtool.IMGTOOL_VAR: "imgtool"}
    )
    with pytest.raises(BuildError) as caught:
        imgtool.run_signing(plan, runner=lambda command: (2, "Image size too large"))
    assert "Image size too large" in caught.value.hint


def test_signing_never_generates_a_key(tmp_path, monkeypatch) -> None:
    """A build has to be signed with the key its bootloader already carries."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty"))
    with pytest.raises(BuildError) as caught:
        imgtool.sign_build(
            _build_dir(tmp_path),
            env=dict(os.environ),
            topdir=workspace.find_topdir(workspace.installed_module_dir(), tmp_path),
        )
    assert "MCUHome has no firmware signing key" in caught.value.message


def test_the_workspace_it_was_given_is_the_one_it_signs_with(tmp_path) -> None:
    """*topdir* reaches :func:`find_imgtool`, or the tool version drifts.

    Dropping the forwarding is invisible: signing keeps working, out of
    whatever ``imgtool`` is on ``PATH`` — a different version from the
    one the build used, on an image whose header that version writes.
    ``PATH`` is emptied here so the workspace is the only answer left,
    and a fallback would show up as a refusal rather than as silence.
    """
    out_dir = _build_dir(tmp_path)
    key = _key(tmp_path)
    topdir = tmp_path / "workspace"
    script = topdir / imgtool.MCUBOOT_IMGTOOL
    script.parent.mkdir(parents=True)
    script.write_text("", "utf-8")

    ran: list[tuple[str, ...]] = []
    plan = imgtool.sign_build(
        out_dir,
        env={"PATH": str(tmp_path / "nothing-here")},
        topdir=topdir,
        key=key,
        runner=lambda command: (ran.append(tuple(command)), (0, ""))[1],
    )
    assert plan.commands[0][1][:2] == (sys.executable, str(script))
    assert ran and ran[0][:2] == (sys.executable, str(script))


# --------------------------------------------------------------------------
# The equivalence proof
# --------------------------------------------------------------------------

#: ``struct image_header`` of ``bootutil/image.h``: magic, load address,
#: header size, protected TLV size, image size, flags, version, padding.
_HEADER = struct.Struct("<IIHHIIBBHIH2x")
_HEADER_MAGIC = 0x96F3B83D
_TLV_INFO = struct.Struct("<HH")
_TLV_INFO_MAGIC = 0x6907
_TLV_PROT_INFO_MAGIC = 0x6908
_TLV = struct.Struct("<BBH")
#: ``IMAGE_TLV_ECDSASIG`` — the one field two signings of one image may
#: differ in.
_TLV_ECDSA_SIG = 0x22
#: ``IMAGE_TLV_SHA256`` — the digest over header and payload, which they
#: may not.
_TLV_SHA256 = 0x10


def _parse_image(data: bytes) -> dict:
    """An MCUboot image, taken apart far enough to compare two of them."""
    magic, _load, hdr_size, prot_size, img_size, flags, *_rest = _HEADER.unpack_from(data)
    assert magic == _HEADER_MAGIC
    end = hdr_size + img_size
    parsed = {
        "header": data[:hdr_size],
        "payload": data[hdr_size:end],
        "flags": flags,
        "protected": data[end : end + prot_size],
        "tlvs": [],
    }
    offset = end + prot_size
    info_magic, total = _TLV_INFO.unpack_from(data, offset)
    assert info_magic in (_TLV_INFO_MAGIC, _TLV_PROT_INFO_MAGIC)
    cursor = offset + _TLV_INFO.size
    while cursor < offset + total:
        kind, _pad, length = _TLV.unpack_from(data, cursor)
        cursor += _TLV.size
        parsed["tlvs"].append((kind, data[cursor : cursor + length]))
        cursor += length
    return parsed


def _imgtool_or_skip() -> list[str]:
    topdir = workspace.find_topdir(workspace.installed_module_dir())
    program = imgtool.find_imgtool(env=dict(os.environ), topdir=topdir)
    if program is None:  # pragma: no cover - depends on the machine
        pytest.skip("imgtool is not available here")
    probe = subprocess.run([*program, "sign", "--help"], capture_output=True, check=False)
    if probe.returncode != 0:  # pragma: no cover - depends on the machine
        pytest.skip("imgtool cannot run here (its dependencies are missing)")
    return program


def test_two_signatures_of_the_same_image_differ_only_there(tmp_path) -> None:
    """The detached-signing equivalence, with a real imgtool.

    The two runs stand in for "inline" and "detached": same tool, same
    arguments, same input bytes, different invocation. Everything MCUboot
    verifies has to match; the signature is allowed to differ, and does,
    because ECDSA is randomized.
    """
    program = _imgtool_or_skip()
    key = _key(tmp_path)
    source = tmp_path / "zephyr.bin"
    # A Zephyr image already carries the MCUboot header's space: the
    # linker reserved CONFIG_ROM_START_OFFSET at the front, which is why
    # neither the inline nor the detached command passes --pad-header.
    source.write_bytes(bytes(PARAMETERS.header_size) + bytes(range(256)) * 8)

    signed = []
    for name in ("inline.bin", "detached.bin"):
        output = tmp_path / name
        command = imgtool.sign_command(
            program, parameters=PARAMETERS, key=key, source=source, output=output
        )
        assert subprocess.run(command, capture_output=True, check=False).returncode == 0
        signed.append(_parse_image(output.read_bytes()))

    inline, detached = signed
    assert inline["header"] == detached["header"]
    assert inline["payload"] == detached["payload"]
    assert inline["flags"] == detached["flags"]
    assert inline["protected"] == detached["protected"]
    assert [kind for kind, _ in inline["tlvs"]] == [kind for kind, _ in detached["tlvs"]]

    digests = [dict(image["tlvs"])[_TLV_SHA256] for image in signed]
    assert digests[0] == digests[1], "the two images are not the same image"
    for (kind, left), (_, right) in zip(inline["tlvs"], detached["tlvs"], strict=True):
        if kind == _TLV_ECDSA_SIG:
            continue
        assert left == right, f"TLV {kind:#04x} differs"

    signatures = [dict(image["tlvs"])[_TLV_ECDSA_SIG] for image in signed]
    assert signatures[0] != signatures[1], "ECDSA signatures are randomized; these were not"


def test_a_detached_signature_verifies_against_the_same_key(tmp_path) -> None:
    """imgtool's own verdict, which is MCUboot's: the signature is good."""
    program = _imgtool_or_skip()
    key = _key(tmp_path)
    source = tmp_path / "zephyr.bin"
    source.write_bytes(bytes(PARAMETERS.header_size) + bytes(range(256)) * 8)
    output = tmp_path / "zephyr.signed.bin"
    subprocess.run(
        imgtool.sign_command(program, parameters=PARAMETERS, key=key, source=source, output=output),
        capture_output=True,
        check=True,
    )
    verified = subprocess.run(
        [*program, "verify", "--key", str(key), str(output)], capture_output=True, check=False
    )
    assert verified.returncode == 0, verified.stdout + verified.stderr


# --------------------------------------------------------------------------
# The §7.2.1 build report — the container backend's leaner report shape
# --------------------------------------------------------------------------


def _report(**overrides) -> dict:
    report = {
        "report": imgtool.REPORT_VERSION,
        "signing": {
            "signature_type": "ecdsa-p256",
            "arguments": {
                "version": "1.2.3+4",
                "header-size": 512,
                "align": 4,
                "slot-size": 933888,
            },
        },
        "memory": [{"image": "app", "region": "FLASH", "used": 1, "total": 2, "percent": 50.0}],
    }
    report.update(overrides)
    return report


def _report_dir(tmp_path: Path, *, report: dict | None = None) -> Path:
    out = tmp_path / "out"
    out.mkdir()
    (out / "firmware.bin").write_bytes(bytes(16))
    (out / "firmware.hex").write_text(":00000001FF\n", "utf-8")
    (out / imgtool.BUILD_REPORT_FILE).write_text(json.dumps(report or _report()), "utf-8")
    return out


def test_read_build_report_accepts_the_reference_shape(tmp_path) -> None:
    out = _report_dir(tmp_path)
    data = imgtool.read_build_report(out / imgtool.BUILD_REPORT_FILE)
    assert data["signing"]["arguments"]["slot-size"] == 933888


def test_read_build_report_refuses_a_foreign_version(tmp_path) -> None:
    out = _report_dir(tmp_path, report=_report(report=2))
    with pytest.raises(BuildError) as caught:
        imgtool.read_build_report(out / imgtool.BUILD_REPORT_FILE)
    assert "report format version 2" in caught.value.message
    assert str(imgtool.REPORT_VERSION) in caught.value.message


def test_read_build_report_refuses_a_report_without_a_signing_block(tmp_path) -> None:
    report = _report()
    del report["signing"]
    out = _report_dir(tmp_path, report=report)
    with pytest.raises(BuildError) as caught:
        imgtool.read_build_report(out / imgtool.BUILD_REPORT_FILE)
    assert "no signing parameters" in caught.value.message


def test_read_build_report_refuses_a_file_that_is_not_json(tmp_path) -> None:
    out = _report_dir(tmp_path)
    (out / imgtool.BUILD_REPORT_FILE).write_text("this is not json {", "utf-8")
    with pytest.raises(BuildError) as caught:
        imgtool.read_build_report(out / imgtool.BUILD_REPORT_FILE)
    assert "not valid JSON" in caught.value.message


def test_read_build_report_refuses_a_report_that_is_not_an_object(tmp_path) -> None:
    out = _report_dir(tmp_path)
    (out / imgtool.BUILD_REPORT_FILE).write_text("[1, 2, 3]", "utf-8")
    with pytest.raises(BuildError) as caught:
        imgtool.read_build_report(out / imgtool.BUILD_REPORT_FILE)
    assert "does not describe a build" in caught.value.message


def test_read_build_report_refuses_malformed_arguments(tmp_path) -> None:
    """A signing block whose arguments are not an object is truncated, not signable."""
    report = _report()
    report["signing"]["arguments"] = "not-an-object"
    out = _report_dir(tmp_path, report=report)
    with pytest.raises(BuildError) as caught:
        imgtool.read_build_report(out / imgtool.BUILD_REPORT_FILE)
    assert "no signing parameters" in caught.value.message


def test_read_build_report_refuses_a_missing_signature_type(tmp_path) -> None:
    """§7.2.1 makes signature_type mandatory so a client can refuse a mismatched key."""
    report = _report()
    del report["signing"]["signature_type"]
    out = _report_dir(tmp_path, report=report)
    with pytest.raises(BuildError) as caught:
        imgtool.read_build_report(out / imgtool.BUILD_REPORT_FILE)
    assert "signature_type" in caught.value.message
    assert SIGNATURE_TYPE in caught.value.message


def test_read_build_report_refuses_a_wrong_signature_type(tmp_path) -> None:
    """A report that signs with another algorithm is refused, not signed anyway."""
    report = _report(
        signing={
            "signature_type": "rsa-2048",
            "arguments": {
                "version": "1.2.3+4",
                "header-size": 512,
                "align": 4,
                "slot-size": 933888,
            },
        }
    )
    out = _report_dir(tmp_path, report=report)
    with pytest.raises(BuildError) as caught:
        imgtool.read_build_report(out / imgtool.BUILD_REPORT_FILE)
    assert "rsa-2048" in caught.value.message
    assert SIGNATURE_TYPE in caught.value.message


def test_plan_report_signing_signs_both_firmware_encodings(tmp_path) -> None:
    """The §7.2.1 parameters apply to every firmware artifact: bin and hex."""
    out = _report_dir(tmp_path)
    key = _key(tmp_path)
    plan = imgtool.plan_report_signing(out, key=key, env={imgtool.IMGTOOL_VAR: "imgtool"})
    assert {path.name for path in plan.outputs} == {"firmware.signed.bin", "firmware.signed.hex"}
    for _form, command, _dest in plan.commands:
        assert command[command.index("--version") + 1] == "1.2.3+4"
        assert command[command.index("--slot-size") + 1] == "933888"
        assert command[command.index("--header-size") + 1] == "512"
        assert command[command.index("--align") + 1] == "4"
        assert command[command.index("--key") + 1] == str(key)


def test_plan_report_signing_needs_a_firmware_to_sign(tmp_path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (out / imgtool.BUILD_REPORT_FILE).write_text(json.dumps(_report()), "utf-8")
    with pytest.raises(BuildError) as caught:
        imgtool.plan_report_signing(out, key=_key(tmp_path), env={imgtool.IMGTOOL_VAR: "imgtool"})
    assert "firmware" in caught.value.message


def test_sign_report_runs_the_plan_and_never_generates_a_key(tmp_path) -> None:
    out = _report_dir(tmp_path)
    commands: list[list[str]] = []

    def runner(command: list[str]) -> tuple[int, str]:
        commands.append(command)
        Path(command[-1]).write_bytes(b"signed")
        return 0, ""

    key = _key(tmp_path)
    plan = imgtool.sign_report(out, env={imgtool.IMGTOOL_VAR: "imgtool"}, key=key, runner=runner)
    assert len(commands) == 2
    assert (out / "firmware.signed.bin").is_file()
    assert (out / "firmware.signed.hex").is_file()
    assert plan.parameters.slot_size == 933888


def test_a_project_key_signs_with_the_referenced_file_and_the_plan_names_it(
    tmp_path,
) -> None:
    """imgtool gets the project's ``mcuboot.pem`` itself.

    Nothing is materialized and nothing cleaned up (PO 2026-08-14): the
    ``!file`` reference in the secrets YAML resolves to a real file, that
    file is the ``--key`` argument, and the returned plan carries the
    same durable path — the one a caller prints to a user after the
    fact, and the one that is still there when they look.
    """
    from mcuhome.workbench.project import init_project

    out = _report_dir(tmp_path)
    project = init_project(tmp_path / "project").project
    generated = signing.signing_key(env={}, project=project)
    used_keys: list[Path] = []

    def runner(command: list[str]) -> tuple[int, str]:
        key_arg = Path(command[command.index("--key") + 1])
        used_keys.append(key_arg)
        assert key_arg.is_file()
        Path(command[-1]).write_bytes(b"signed")
        return 0, ""

    plan = imgtool.sign_report(
        out, env={imgtool.IMGTOOL_VAR: "imgtool"}, project=project, runner=runner
    )
    assert used_keys and all(path == generated.path for path in used_keys)
    assert generated.path.is_file()  # the durable home, untouched
    assert plan.key == generated.path
    assert plan.key.name == signing.PRIVATE_KEY_FILE
    assert plan.key != project.firmware_secrets_file  # the YAML is never a --key


def test_sign_report_refuses_a_missing_key_rather_than_making_one(tmp_path) -> None:
    """A delivered build is signed with the key its bootloader carries, not a fresh one."""
    out = _report_dir(tmp_path)
    empty = tmp_path / "empty"
    with pytest.raises(BuildError):
        imgtool.sign_report(
            out, env={"XDG_CONFIG_HOME": str(empty), imgtool.IMGTOOL_VAR: "imgtool"}, key=None
        )
    assert not (out / "firmware.signed.bin").exists()
