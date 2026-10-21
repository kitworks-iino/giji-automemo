from email.message import Message
import io
import json
import struct
from types import SimpleNamespace
import zipfile

import pytest

from giji.runtime_dependencies import ProhibitedDependencyError, ensure_no_prohibited_dependencies
from scripts.audit_dependencies import DistributionAudit, audit_archive, pe_import_names


def distribution(name, *requirements):
    info = Message()
    info["Name"] = name
    for requirement in requirements:
        info["Requires-Dist"] = requirement
    return SimpleNamespace(metadata=info, version="1.0")


@pytest.mark.parametrize("name", ["av", "PyAV", "faster_whisper", "ctranslate2", "imageio-ffmpeg",
                                  "ffmpeg-python", "moviepy", "opencv-python-headless"])
def test_legacy_installed_packages_are_rejected_even_if_the_app_does_not_import_them(name):
    with pytest.raises(ProhibitedDependencyError, match="new virtual environment"):
        ensure_no_prohibited_dependencies([distribution(name)])


def test_transitive_declaration_is_checked_and_clean_onnx_stack_is_allowed():
    with pytest.raises(ProhibitedDependencyError, match="declares a prohibited dependency"):
        ensure_no_prohibited_dependencies([distribution("some-wrapper", "av>=14")])
    records = ensure_no_prohibited_dependencies([
        distribution("sherpa-onnx", "sherpa-onnx-core==1.13.8"), distribution("sherpa-onnx-core"),
        distribution("scipy", "numpy>=1.25"), distribution("numpy"),
    ])
    assert len(records) == 4


def pe_fixture(normal="KERNEL32.dll", delayed=None):
    payload = bytearray(0x800)
    payload[:2] = b"MZ"
    struct.pack_into("<I", payload, 0x3C, 0x80)
    payload[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H", payload, 0x86, 1)
    struct.pack_into("<H", payload, 0x94, 240)
    optional = 0x98
    struct.pack_into("<H", payload, optional, 0x20B)
    struct.pack_into("<Q", payload, optional + 24, 0x140000000)
    struct.pack_into("<I", payload, optional + 60, 0x200)
    struct.pack_into("<I", payload, optional + 108, 16)
    struct.pack_into("<II", payload, optional + 112 + 8, 0x1000, 40)
    struct.pack_into("<IIII", payload, optional + 240 + 8, 0x600, 0x1000, 0x600, 0x200)
    struct.pack_into("<IIIII", payload, 0x200, 0, 0, 0, 0x1050, 0)
    payload[0x250:0x250 + len(normal) + 1] = normal.encode() + b"\0"
    if delayed:
        struct.pack_into("<II", payload, optional + 112 + 13 * 8, 0x1100, 64)
        struct.pack_into("<IIIIIIII", payload, 0x300, 1, 0x1150, 0, 0, 0, 0, 0, 0)
        payload[0x350:0x350 + len(delayed) + 1] = delayed.encode() + b"\0"
    return bytes(payload)


def test_pe_import_parser_checks_normal_and_delayed_libraries():
    assert pe_import_names(pe_fixture(delayed="USER32.dll")) == ["KERNEL32.dll", "USER32.dll"]


@pytest.mark.parametrize("payload", [
    pe_fixture(normal="avcodec-62.dll"), pe_fixture(delayed="swresample-6.dll"),
], ids=["normal-import", "delayed-import"])
def test_renamed_native_wrapper_is_rejected_by_its_pe_imports(payload):
    with pytest.raises(ProhibitedDependencyError, match="Prohibited PE import"):
        DistributionAudit().inspect("renamed-innocent.dll", payload)


def test_renamed_library_is_rejected_by_exported_function_identifiers():
    with pytest.raises(ProhibitedDependencyError, match="native audio identifier"):
        DistributionAudit().inspect("other-name.dll", pe_fixture() + b"\0avcodec_version\0")


def test_standard_library_format_error_symbol_and_policy_documentation_are_allowed():
    audit = DistributionAudit()
    audit.inspect("onnxruntime.dll", pe_fixture() + b".?AVformat_error@std@@\0")
    audit.inspect("docs/audio-dependencies.md", b"FFmpeg, PyAV and faster-whisper are prohibited.")
    assert audit.report()["passed"]


def test_unreadable_pe_imports_fail_closed():
    with pytest.raises(ProhibitedDependencyError, match="PE imports could not be verified"):
        DistributionAudit().inspect("broken.dll", b"MZ" + b"\0" * 60)


def test_ffmpeg_source_inside_nested_archive_cannot_hide_in_a_portable_zip(tmp_path):
    nested = io.BytesIO()
    with zipfile.ZipFile(nested, "w") as archive:
        archive.writestr("media-source/libavcodec/decode.c", "prohibited source fixture")
    output = tmp_path / "public.zip"
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("GijiAutoMemo/third-party-sources/other-name.zip", nested.getvalue())
    with pytest.raises(ProhibitedDependencyError, match="Prohibited audio component"):
        audit_archive(output)


def test_renamed_distribution_metadata_and_npm_wrappers_are_rejected():
    with pytest.raises(ProhibitedDependencyError, match="package metadata"):
        DistributionAudit().inspect("licenses/renamed/METADATA.txt", b"Name: av\nVersion: 16.1.0\n")
    with pytest.raises(ProhibitedDependencyError, match="frontend package"):
        DistributionAudit().inspect("frontend/package.json", json.dumps({
            "name": "test", "dependencies": {"@ffmpeg/core": "1.0"},
        }).encode())


def test_renaming_native_binary_to_an_onnx_file_does_not_bypass_audit():
    source = io.BytesIO(pe_fixture(normal="avformat-62.dll"))
    with pytest.raises(ProhibitedDependencyError, match="PE import"):
        DistributionAudit().inspect_stream("weights.onnx", source)
