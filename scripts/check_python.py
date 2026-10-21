"""Validate a source/build interpreter before installing native dependencies."""

from __future__ import annotations

import argparse
import struct
import sys
import sysconfig


def runtime_error(info: dict, required_minor: str | None = None) -> str | None:
    if info["platform"] != "win32":
        return "Windows is required."
    if info["implementation"] != "cpython":
        return "Use standard CPython; alternative Python implementations are not supported."
    if info["bits"] != 64 or info["platform_tag"] != "win-amd64":
        return "Use the Windows x64 (64-bit) Python installer."
    if info["free_threaded"]:
        return "Use standard Python, not the experimental free-threaded build."
    version = tuple(info["version"][:2])
    if version not in {(3, 11), (3, 12), (3, 13)}:
        return "Python 3.13 x64 is recommended; Python 3.11 and 3.12 x64 are also supported."
    if required_minor and version != tuple(int(part) for part in required_minor.split(".")):
        return f"Python {required_minor} x64 is required for this operation."
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require-minor", choices=("3.11", "3.12", "3.13"))
    args = parser.parse_args()
    error = runtime_error({
        "platform": sys.platform,
        "implementation": sys.implementation.name,
        "bits": struct.calcsize("P") * 8,
        "platform_tag": sysconfig.get_platform(),
        "version": sys.version_info[:2],
        "free_threaded": bool(sysconfig.get_config_var("Py_GIL_DISABLED")),
    }, args.require_minor)
    if error:
        print(error, file=sys.stderr)
        return 1
    # Output only the path so PowerShell can invoke a selected installation,
    # even when it lives in a directory containing spaces.
    print(sys.executable)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
