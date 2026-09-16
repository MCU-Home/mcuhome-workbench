# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Detached signing, and the proof that it is the same signature.

The claim is that ``mcuhome sign`` produces what
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
from mcuhome.model.errors import BuildError
from mcuhome.model.registry import SIGNATURE_TYPE
from mcuhome.model.signing import SigningParameters

from mcuhome.workbench import imgtool, signing

PARAMETERS = SigningParameters(header_size=512, align=4, slot_size=933888, version="0.0.0+0")

#: A P-256 key with a known scalar, so the suite never draws one and
#: never touches the developer's own (see conftest's autouse fixture).
TEST_SCALAR = 0x00112233445566778899AABBCCDDEEFF00112233445566778899AABBCCDDEEF0


# --------------------------------------------------------------------------
# Finding imgtool
# --------------------------------------------------------------------------


def test_the_installed_package_wins(tmp_path) -> None:
    """imgtool is a declared dependency: its console script sits beside
    this interpreter, and that is the one a plain environment gets."""
    beside = Path(sys.executable).parent / "imgtool"
    assert beside.is_file(), "imgtool is a dependency of mcuhome-workbench"
    assert imgtool.find_imgtool(env={"PATH": str(tmp_path)}) == [str(beside)]


def test_the_stated_program_beats_the_installed_package(tmp_path) -> None:
    other = tmp_path / "other-imgtool.py"
    other.write_text("", "utf-8")
    found = imgtool.find_imgtool(env={}, stated=str(other))
    assert found == [sys.executable, str(other)]


def test_a_program_name_is_taken_as_a_program() -> None:
    assert imgtool.find_imgtool(env={}, stated="imgtool") == ["imgtool"]


def test_path_answers_when_no_script_sits_beside_the_interpreter(tmp_path, monkeypatch) -> None:
    """A system install may put the console script elsewhere on PATH."""
    monkeypatch.setattr(imgtool.sys, "executable", str(tmp_path / "venv" / "bin" / "python"))
    elsewhere = tmp_path / "bin"
    elsewhere.mkdir()
    program = elsewhere / "imgtool"
    program.write_text("", "utf-8")
    program.chmod(0o755)
    assert imgtool.find_imgtool(env={"PATH": str(elsewhere)}) == [str(program)]


def test_no_imgtool_anywhere_is_a_refusal_that_names_the_dependency(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(imgtool.sys, "executable", str(tmp_path / "venv" / "bin" / "python"))
    with pytest.raises(BuildError) as caught:
        imgtool.require_imgtool(env={"PATH": str(tmp_path)})
    assert "declared dependency" in caught.value.hint
    assert "mcuhome-workbench" in caught.value.hint
    assert "signing.imgtool" in caught.value.hint


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
# The key the suite signs with
# --------------------------------------------------------------------------


def _key(tmp_path: Path) -> Path:
    path = tmp_path / "signing.key"
    path.write_text(signing.generate_key_pem(TEST_SCALAR), "utf-8")
    path.chmod(0o600)
    return path


def _fake_imgtool(tmp_path: Path, *, exit_code: int = 0, says: str = "", writes: bool = True):
    """A signing program that records its argv, in a real process.

    The seam the signing tests need is not a callable inside this
    process: `sign_firmware` starts the program itself, so a double that
    is a function would leave exactly the step that can go wrong
    untested. This writes a script `find_imgtool` accepts (a ``.py``
    runs on this interpreter) and answers a reader for what it was
    called with.
    """
    log = tmp_path / "imgtool-calls.json"
    script = tmp_path / "fake-imgtool.py"
    script.write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        f"log = Path({str(log)!r})\n"
        "calls = json.loads(log.read_text()) if log.exists() else []\n"
        "calls.append(sys.argv)\n"
        "log.write_text(json.dumps(calls))\n"
        f"if {writes!r}:\n"
        "    Path(sys.argv[-1]).write_bytes(Path(sys.argv[-2]).read_bytes() + b'-signed')\n"
        f"print({says!r})\n"
        f"sys.exit({exit_code})\n",
        "utf-8",
    )

    def calls() -> list[list[str]]:
        return json.loads(log.read_text("utf-8")) if log.exists() else []

    return script, calls


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
    program = imgtool.find_imgtool(env=dict(os.environ))
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
# The build report, in the build actions document's shape — leaner for the container backend
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
    """The build actions document's report shape makes signature_type
    mandatory so a client can refuse a mismatched key.
    """
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


def test_the_memory_footprint_is_read_out_of_the_report(tmp_path) -> None:
    """The figures a build measured, typed, in the report's own spelling.

    Every client that showed them used to parse the list itself, so the
    same measurement had a different shape in each of them.
    """
    report = imgtool.read_build_report(_report_dir(tmp_path) / imgtool.BUILD_REPORT_FILE)

    (region,) = imgtool.memory_footprint(report)

    assert (region.image, region.region, region.used, region.total) == ("app", "FLASH", 1, 2)
    document = region.to_dict()
    assert document == {"image": "app", "region": "FLASH", "used": 1, "total": 2}
    assert "percent" not in document, "a derived value stated twice can contradict itself"
    assert json.dumps(document)


def test_a_report_that_measured_nothing_has_no_footprint() -> None:
    """``memory`` is optional: a build that relinked nothing states none."""
    report = _report()
    del report["memory"]

    assert imgtool.memory_footprint(report) == ()
    assert imgtool.memory_footprint({"memory": "not a list"}) == ()


def test_an_entry_without_numbers_is_left_out_rather_than_zeroed() -> None:
    """A figure this package invented would be read as one a build measured."""
    regions = imgtool.memory_footprint(
        {
            "memory": [
                {"image": "app", "region": "FLASH", "used": "lots", "total": 2},
                {"image": "app", "region": "RAM"},
                "not an object",
                # A bool is an int in Python and is a byte count nowhere.
                {"image": "app", "region": "RAM", "used": True, "total": 4},
                {"image": "app", "region": "RAM", "used": 3, "total": 4},
            ]
        }
    )

    assert [(region.region, region.used) for region in regions] == [("RAM", 3)]


def test_plan_signing_signs_both_firmware_encodings(tmp_path) -> None:
    """The build actions document's report-shape parameters apply to every
    firmware artifact: bin and hex.
    """
    out = _report_dir(tmp_path)
    key = _key(tmp_path)
    plan = imgtool.plan_signing(out, key=key, env={}, imgtool="imgtool")
    assert {path.name for path in plan.outputs} == {"firmware.signed.bin", "firmware.signed.hex"}
    for _form, command, _dest in plan.commands:
        assert command[command.index("--version") + 1] == "1.2.3+4"
        assert command[command.index("--slot-size") + 1] == "933888"
        assert command[command.index("--header-size") + 1] == "512"
        assert command[command.index("--align") + 1] == "4"
        assert command[command.index("--key") + 1] == str(key)


def test_plan_signing_needs_a_firmware_to_sign(tmp_path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (out / imgtool.BUILD_REPORT_FILE).write_text(json.dumps(_report()), "utf-8")
    with pytest.raises(BuildError) as caught:
        imgtool.plan_signing(out, key=_key(tmp_path), env={}, imgtool="imgtool")
    assert "firmware" in caught.value.message


def test_sign_firmware_runs_exactly_the_plan_it_decided(tmp_path) -> None:
    """The result is what the run produced, and the run is the plan.

    Both halves are checked against a real signing program: what it was
    called with has to be the argv the plan carries, command for command
    — a result assembled from a directory listing would pass a weaker
    test and be wrong the first time a build directory holds a file from
    an earlier run.
    """
    out = _report_dir(tmp_path)
    key = _key(tmp_path)
    program, calls = _fake_imgtool(tmp_path)

    plan = imgtool.plan_signing(out, env={}, key=key, imgtool=str(program))
    result = imgtool.sign_firmware(out, env={}, key=key, imgtool=str(program))

    assert result.ok
    assert result.out_dir == out
    assert result.report_path == out / imgtool.BUILD_REPORT_FILE
    assert result.key == key
    assert [artifact.format for artifact in result.signed] == ["bin", "hex"]
    assert [artifact.path for artifact in result.signed] == plan.outputs
    assert all(artifact.path.is_file() for artifact in result.signed)
    # The program sees its own path as argv[0], the plan carries the
    # interpreter in front of it — the rest has to be identical.
    assert [list(argv)[1:] for _form, argv, _output in plan.commands] == calls()


def test_a_signing_program_that_writes_nothing_is_not_an_ok_result(tmp_path) -> None:
    """``ok`` is a fact about the files, not a word the result was born with."""
    out = _report_dir(tmp_path)
    program, _calls = _fake_imgtool(tmp_path, writes=False)
    result = imgtool.sign_firmware(out, env={}, key=_key(tmp_path), imgtool=str(program))
    assert not result.ok
    assert result.signed  # it said what it meant to produce
    assert not any(artifact.path.exists() for artifact in result.signed)


def test_the_signing_document_is_what_a_client_prints(tmp_path) -> None:
    out = _report_dir(tmp_path)
    program, _calls = _fake_imgtool(tmp_path)
    result = imgtool.sign_firmware(out, env={}, key=_key(tmp_path), imgtool=str(program))
    document = result.to_dict()
    assert list(document) == ["ok", "out_dir", "report_path", "key", "signed"]
    assert document["signed"] == [
        {"format": "bin", "path": str(out / "firmware.signed.bin")},
        {"format": "hex", "path": str(out / "firmware.signed.hex")},
    ]
    assert json.dumps(document)


def test_a_project_key_signs_with_the_referenced_file_and_the_plan_names_it(
    tmp_path,
) -> None:
    """imgtool gets the project's own key file itself.

    Nothing is materialized and nothing cleaned up: the ``!file``
    reference in the secrets YAML resolves to a real file, that file is
    the ``--key`` argument, and the result carries the same durable path
    — the one a caller prints to a user after the fact, and the one that
    is still there when they look.
    """
    from mcuhome.workbench.project import create_project

    out = _report_dir(tmp_path)
    project = create_project(tmp_path / "project").project
    generated = signing.create_signing_key(env={}, project=project)
    program, calls = _fake_imgtool(tmp_path)

    result = imgtool.sign_firmware(out, env={}, project=project, imgtool=str(program))

    used = {Path(argv[argv.index("--key") + 1]) for argv in calls()}
    assert used == {generated.path}
    assert generated.path.is_file()  # the durable home, untouched
    assert result.key == generated.path
    assert result.key.name == signing.SIGNING_KEY_FILE
    assert result.key != project.signing_secrets_file  # the YAML is never a --key


def test_signing_refuses_a_missing_key_rather_than_making_one(tmp_path) -> None:
    """A delivered build is signed with the key its bootloader carries, not a fresh one."""
    from mcuhome.workbench.project import create_project

    out = _report_dir(tmp_path)
    project = create_project(tmp_path / "project").project
    program, calls = _fake_imgtool(tmp_path)
    for call in (imgtool.plan_signing, imgtool.sign_firmware):
        with pytest.raises(BuildError) as caught:
            call(out, env={}, project=project, imgtool=str(program))
        assert "no firmware signing key yet" in caught.value.message
    assert not (out / "firmware.signed.bin").exists()
    assert not calls(), "nothing may run before the key is there"
    assert not project.signing_secrets_file.exists()


def test_imgtool_failure_carries_imgtools_own_words(tmp_path) -> None:
    out = _report_dir(tmp_path)
    program, _calls = _fake_imgtool(tmp_path, exit_code=2, says="Image size too large")
    with pytest.raises(BuildError) as caught:
        imgtool.sign_firmware(out, env={}, key=_key(tmp_path), imgtool=str(program))
    assert "Image size too large" in caught.value.hint
    assert "--key" in caught.value.hint  # the command, so it can be run by hand


def test_the_installed_imgtool_is_the_one_it_signs_with(tmp_path) -> None:
    """Signing runs the declared dependency, not an environment accident.

    ``PATH`` is emptied so the console script beside this interpreter is
    the only answer left — a fallback to anything else would show up as
    a different argv, and a lost dependency as a refusal. What runs is
    what the plan carries, which the test above pins against a real
    process.
    """
    out = _report_dir(tmp_path)
    beside = Path(sys.executable).parent / "imgtool"
    plan = imgtool.plan_signing(
        out, env={"PATH": str(tmp_path / "nothing-here")}, key=_key(tmp_path)
    )
    assert plan.commands[0][1][0] == str(beside)


def test_the_plan_document_shows_the_commands_before_they_run(tmp_path) -> None:
    out = _report_dir(tmp_path)
    key = _key(tmp_path)
    plan = imgtool.plan_signing(out, env={}, key=key, imgtool="imgtool")
    document = plan.to_dict()
    assert list(document) == ["out_dir", "report_path", "key", "commands"]
    assert document["key"] == str(key)
    formats = [command["format"] for command in document["commands"]]
    assert formats == ["bin", "hex"]
    for command, (_form, argv, output) in zip(document["commands"], plan.commands, strict=True):
        assert command["argv"] == list(argv)
        assert command["output"] == str(output)
    assert json.dumps(document)


def test_the_runner_seam_reports_what_the_program_printed(tmp_path) -> None:
    """The in-process seam the suite uses where a real process buys nothing."""
    plan = imgtool.plan_signing(
        _report_dir(tmp_path), key=_key(tmp_path), env={}, imgtool="imgtool"
    )
    with pytest.raises(BuildError) as caught:
        imgtool.run_signing(plan, runner=lambda command: (2, "Slot size too small"))
    assert "Slot size too small" in caught.value.hint
