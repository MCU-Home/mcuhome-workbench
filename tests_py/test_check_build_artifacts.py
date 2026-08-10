# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The CI artifact gate: a complete build passes, a broken one is named.

``scripts/check_build_artifacts.py`` is the step behind the Matter build
job that turns "the compiler exited 0" into "the flashable files exist and
the build's description of itself is well-formed". It handles two build
shapes, chosen by which description file is present:

* the **default** container path delivers ``build-report.json`` beside the
  flat ``firmware.*`` / ``firmware.signed.*`` / ``*.ota`` set;
* ``--native`` writes the fuller ``build-manifest.json`` over a sysbuild
  layout, with a size and SHA-256 per file.

The gate is only worth having if it fails on the very outputs a green-but-
empty build leaves behind, so every test here builds a complete fake build
directory of one shape and then breaks exactly one thing.

The script lives in ``scripts/``, which is not a package, so it is loaded
by path — the same way ``test_sdk_archive.py`` loads its script.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check_build_artifacts.py"


def _script():
    """``check_build_artifacts.py`` as a module — ``scripts/`` is not a package."""
    spec = importlib.util.spec_from_file_location("check_build_artifacts", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def script():
    return _script()


# --- the default (container / build-report.json) shape ------------------


def _report(**overrides) -> dict:
    report = {
        "report": 1,
        "signing": {
            "signature_type": "ecdsa-p256",
            "arguments": {
                "version": "1.0.0+0",
                "header-size": 512,
                "align": 4,
                "slot-size": 983040,
            },
        },
        "memory": [],
    }
    report.update(overrides)
    return report


def make_report_dir(tmp: Path, *, bootloader: bool = True, report: dict | None = None) -> Path:
    """A complete container-path build directory."""
    (tmp / "build-report.json").write_text(json.dumps(_report() if report is None else report))
    for name in ("firmware.hex", "firmware.bin", "firmware.signed.hex", "firmware.signed.bin"):
        (tmp / name).write_bytes(b"payload")
    if bootloader:
        (tmp / "bootloader.hex").write_bytes(b"boot")
    (tmp / "bench-node-1.0.0.ota").write_bytes(b"otadata")
    return tmp


def test_report_complete_passes(script, tmp_path):
    assert script.check(make_report_dir(tmp_path)) == []
    assert script.main(["check", str(tmp_path)]) == 0


def test_report_complete_without_bootloader_passes(script, tmp_path):
    # §7.2: a bootloader is optional; firmware + report are what is required.
    assert script.check(make_report_dir(tmp_path, bootloader=False)) == []


@pytest.mark.parametrize(
    "missing",
    ["firmware.hex", "firmware.bin", "firmware.signed.hex", "firmware.signed.bin"],
)
def test_report_missing_firmware_is_a_finding(script, tmp_path, missing):
    make_report_dir(tmp_path)
    (tmp_path / missing).unlink()
    findings = script.check(tmp_path)
    assert any(missing in finding and "missing" in finding for finding in findings)


@pytest.mark.parametrize(
    "empty",
    ["firmware.hex", "firmware.signed.bin", "bench-node-1.0.0.ota"],
)
def test_report_empty_file_is_a_finding(script, tmp_path, empty):
    make_report_dir(tmp_path)
    (tmp_path / empty).write_bytes(b"")
    findings = script.check(tmp_path)
    assert any(empty in finding and "empty" in finding for finding in findings)


def test_report_empty_bootloader_is_a_finding(script, tmp_path):
    make_report_dir(tmp_path)
    (tmp_path / "bootloader.hex").write_bytes(b"")
    findings = script.check(tmp_path)
    assert any("bootloader.hex" in finding and "empty" in finding for finding in findings)


def test_report_no_ota_is_a_finding(script, tmp_path):
    make_report_dir(tmp_path)
    (tmp_path / "bench-node-1.0.0.ota").unlink()
    findings = script.check(tmp_path)
    assert any(".ota" in finding for finding in findings)


def test_report_two_ota_is_a_finding(script, tmp_path):
    make_report_dir(tmp_path)
    (tmp_path / "bench-node-2.0.0.ota").write_bytes(b"second")
    findings = script.check(tmp_path)
    assert any("exactly one .ota" in finding for finding in findings)


def test_report_malformed_json_is_a_finding(script, tmp_path):
    make_report_dir(tmp_path)
    (tmp_path / "build-report.json").write_text("{ not json")
    findings = script.check(tmp_path)
    assert any("build-report.json" in finding and "JSON" in finding for finding in findings)


def test_report_empty_report_file_is_a_finding(script, tmp_path):
    make_report_dir(tmp_path)
    (tmp_path / "build-report.json").write_bytes(b"")
    findings = script.check(tmp_path)
    assert any("build-report.json" in finding for finding in findings)


def test_report_wrong_version_is_a_finding(script, tmp_path):
    make_report_dir(tmp_path, report=_report(report=2))
    findings = script.check(tmp_path)
    assert any("report format version" in finding for finding in findings)


def test_report_wrong_signature_type_is_a_finding(script, tmp_path):
    report = _report()
    report["signing"]["signature_type"] = "ed25519"
    make_report_dir(tmp_path, report=report)
    findings = script.check(tmp_path)
    assert any("signature_type" in finding for finding in findings)


def test_report_missing_signing_argument_is_a_finding(script, tmp_path):
    report = _report()
    del report["signing"]["arguments"]["slot-size"]
    make_report_dir(tmp_path, report=report)
    findings = script.check(tmp_path)
    assert any("slot-size" in finding for finding in findings)


def test_report_arguments_not_an_object_is_a_finding(script, tmp_path):
    report = _report()
    report["signing"]["arguments"] = "512"
    make_report_dir(tmp_path, report=report)
    findings = script.check(tmp_path)
    assert any("arguments" in finding for finding in findings)


def test_report_no_signing_block_is_a_finding(script, tmp_path):
    make_report_dir(tmp_path, report={"report": 1, "memory": []})
    findings = script.check(tmp_path)
    assert any("signing block" in finding for finding in findings)


# --- the --native (build-manifest.json) shape ---------------------------


def _entry(path: Path, out_dir: Path) -> dict:
    data = path.read_bytes()
    return {
        "path": path.relative_to(out_dir).as_posix(),
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def make_manifest_dir(tmp: Path) -> Path:
    """A complete --native build directory, every file hashed into the manifest."""
    layout = {
        "mcuboot/zephyr/zephyr.hex": b"bootloader-image",
        "app/zephyr/zephyr.signed.hex": b"signed-application-hex",
        "app/zephyr/zephyr.signed.bin": b"signed-application-bin",
        "merged.hex": b"merged-image",
        "bench-node.ota": b"ota-image",
    }
    for relative, data in layout.items():
        path = tmp / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    manifest = {
        "images": [
            {
                "name": "mcuboot",
                "role": "bootloader",
                "files": [_entry(tmp / "mcuboot/zephyr/zephyr.hex", tmp)],
            },
            {
                "name": "bmp180_node",
                "role": "application",
                "files": [
                    _entry(tmp / "app/zephyr/zephyr.signed.hex", tmp),
                    _entry(tmp / "app/zephyr/zephyr.signed.bin", tmp),
                ],
            },
        ],
        "merged": _entry(tmp / "merged.hex", tmp),
        "signing": {"signed": True},
        "ota": _entry(tmp / "bench-node.ota", tmp),
        "device": {"name": "bench-node", "board": "nrf7002dk/nrf5340/cpuapp"},
    }
    (tmp / "build-manifest.json").write_text(json.dumps(manifest))
    return tmp


def test_manifest_complete_passes(script, tmp_path):
    assert script.check(make_manifest_dir(tmp_path)) == []
    assert script.main(["check", str(tmp_path)]) == 0


def test_manifest_missing_merged_is_a_finding(script, tmp_path):
    make_manifest_dir(tmp_path)
    manifest = json.loads((tmp_path / "build-manifest.json").read_text())
    del manifest["merged"]
    (tmp_path / "build-manifest.json").write_text(json.dumps(manifest))
    findings = script.check(tmp_path)
    assert any("merged hex" in finding for finding in findings)


def test_manifest_unsigned_is_a_finding(script, tmp_path):
    make_manifest_dir(tmp_path)
    manifest = json.loads((tmp_path / "build-manifest.json").read_text())
    manifest["signing"]["signed"] = False
    (tmp_path / "build-manifest.json").write_text(json.dumps(manifest))
    findings = script.check(tmp_path)
    assert any("unsigned" in finding for finding in findings)


def test_manifest_hash_mismatch_is_a_finding(script, tmp_path):
    make_manifest_dir(tmp_path)
    # Truncate a listed file so the recorded size and SHA-256 no longer hold.
    (tmp_path / "app/zephyr/zephyr.signed.bin").write_bytes(b"tampered")
    findings = script.check(tmp_path)
    assert any("hashes to" in finding for finding in findings)


def test_manifest_missing_application_file_is_a_finding(script, tmp_path):
    make_manifest_dir(tmp_path)
    (tmp_path / "app/zephyr/zephyr.signed.hex").unlink()
    findings = script.check(tmp_path)
    assert any("zephyr.signed.hex" in finding for finding in findings)


def test_manifest_no_bootloader_image_is_a_finding(script, tmp_path):
    make_manifest_dir(tmp_path)
    manifest = json.loads((tmp_path / "build-manifest.json").read_text())
    manifest["images"] = [image for image in manifest["images"] if image["name"] != "mcuboot"]
    (tmp_path / "build-manifest.json").write_text(json.dumps(manifest))
    findings = script.check(tmp_path)
    assert any("no mcuboot image" in finding for finding in findings)


def test_manifest_malformed_json_is_a_finding(script, tmp_path):
    make_manifest_dir(tmp_path)
    (tmp_path / "build-manifest.json").write_text("{ not json")
    findings = script.check(tmp_path)
    assert any("JSON" in finding for finding in findings)


# --- dispatch and usage -------------------------------------------------


def test_neither_description_file_is_a_finding(script, tmp_path):
    findings = script.check(tmp_path)
    assert len(findings) == 1
    assert "not a finished build directory" in findings[0]


def test_report_shape_wins_when_both_present(script, tmp_path):
    # Mutually exclusive in practice, but presence is the documented
    # selector and the report shape is the default path. A manifest that
    # would fail its own shape proves the report shape is the one chosen.
    make_report_dir(tmp_path)
    (tmp_path / "build-manifest.json").write_text("{}")
    assert script.check(tmp_path) == []
    assert "bench-node-1.0.0.ota" in script.describe(tmp_path)


def test_usage_error_exits_2(script, capsys):
    assert script.main(["check"]) == 2


def test_incomplete_build_exits_1(script, tmp_path):
    make_report_dir(tmp_path)
    (tmp_path / "firmware.signed.bin").unlink()
    assert script.main(["check", str(tmp_path)]) == 1
