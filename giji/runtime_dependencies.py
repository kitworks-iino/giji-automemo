"""Reject legacy audio packages that can introduce the prohibited media stack."""

from __future__ import annotations

import importlib.metadata as metadata
import re


PROHIBITED_DISTRIBUTIONS = frozenset({
    "av", "pyav", "faster-whisper", "ctranslate2", "moviepy", "pydub", "audioread",
    "opencv-python", "opencv-python-headless", "opencv-contrib-python",
    "opencv-contrib-python-headless", "torchaudio",
})


class ProhibitedDependencyError(RuntimeError):
    pass


def normalized_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name.strip()).lower()


def prohibited_distribution(name: str) -> bool:
    normalized = normalized_name(name)
    return "ffmpeg" in normalized or normalized in PROHIBITED_DISTRIBUTIONS


def ensure_no_prohibited_dependencies(distributions=None) -> list[dict[str, str]]:
    records = []
    for dist in metadata.distributions() if distributions is None else distributions:
        name = dist.metadata.get("Name", "")
        if prohibited_distribution(name):
            raise ProhibitedDependencyError(
                f"Prohibited legacy audio dependency detected: {normalized_name(name)}. "
                "Use a new virtual environment or the current portable distribution; "
                "upgrading with pip does not remove obsolete packages."
            )
        for requirement in dist.metadata.get_all("Requires-Dist", []):
            match = re.match(r"\s*([A-Za-z0-9_.-]+)", requirement)
            if match and prohibited_distribution(match.group(1)):
                raise ProhibitedDependencyError(
                    f"An installed package declares a prohibited dependency: {normalized_name(match.group(1))}. "
                    "Use a clean virtual environment with this version's dependencies."
                )
        records.append({"name": name, "version": dist.version})
    return sorted(records, key=lambda item: normalized_name(item["name"]))
