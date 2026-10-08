"""Build the ai-dlc-agent-harness wheel and sdist from the platform sources.

    python sdk/agent-harness/build_sdk.py [--out dist/sdk] [--no-isolation]

The platform source tree stays authoritative: files are copied into a temporary staging
directory at build time, never committed twice. Only the modules listed in ``INCLUDE`` ship;
the ``ai_dlc``, ``ai_dlc.application`` and ``ai_dlc.domain`` ``__init__`` files are
deliberately left out so the SDK and the platform can share those namespaces.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parents[1] / "src"

# Smallest extraction boundary: the harness plus the trusted-context contracts it imports.
INCLUDE = (
    "ai_dlc/application/agent_harness",
    "ai_dlc/domain/identity",
    "ai_dlc/domain/authorization.py",
    "ai_dlc/domain/approval.py",
)


def stage(destination: Path) -> None:
    for name in ("pyproject.toml", "README.md"):
        shutil.copy2(HERE / name, destination / name)
    for entry in INCLUDE:
        source = SRC / entry
        target = destination / "src" / entry
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy2(source, target)


def build(out: Path, *, isolation: bool = True) -> list[Path]:
    out = out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ai-dlc-sdk-") as tmp:
        staging = Path(tmp)
        stage(staging)
        command = [sys.executable, "-m", "build", "--wheel", "--sdist", "--outdir", str(out)]
        if not isolation:
            command.append("--no-isolation")
        subprocess.run([*command, str(staging)], check=True)
    return sorted(out.glob("ai_dlc_agent_harness-*"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=HERE.parents[1] / "dist" / "sdk")
    parser.add_argument("--no-isolation", action="store_true", help="use installed setuptools")
    args = parser.parse_args()
    for artifact in build(args.out, isolation=not args.no_isolation):
        print(artifact)
