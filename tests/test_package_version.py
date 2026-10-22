"""``promptise.__version__`` comes from the installed package metadata."""

from __future__ import annotations

import importlib.metadata
import os
import subprocess
import sys
from pathlib import Path

import promptise


def test_version_matches_the_installed_metadata() -> None:
    assert promptise.__version__ == importlib.metadata.version("promptise")


def test_version_without_installed_metadata() -> None:
    code = (
        "import importlib.metadata as m\n"
        "real = m.version\n"
        "def version(name):\n"
        "    if name == 'promptise':\n"
        "        raise m.PackageNotFoundError(name)\n"
        "    return real(name)\n"
        "m.version = version\n"
        "import promptise\n"
        "print(promptise.__version__)\n"
    )
    src = str(Path(promptise.__file__).parent.parent)
    env = {**os.environ, "PYTHONPATH": src + os.pathsep + os.environ.get("PYTHONPATH", "")}
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=120
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "0.0.0+unknown"
