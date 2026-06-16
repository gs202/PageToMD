"""Release-prep packaging tests.

Verifies ``uv build`` produces a clean sdist + wheel, the wheel installs and
runs, and the sdist include/exclude contract holds.
"""

from __future__ import annotations

import os
import pathlib
import re
import subprocess
import tarfile
import venv

import pytest

# Repo root is two parents up from this test file:
# tests/integration/test_packaging.py -> tests/integration -> tests -> repo.
PROJECT_ROOT: pathlib.Path = pathlib.Path(__file__).resolve().parents[2]


def _have_uv() -> bool:
    """Return whether the ``uv`` CLI is reachable on PATH."""
    try:
        return (
            subprocess.run(
                ["uv", "--version"],
                capture_output=True,
                check=False,
            ).returncode
            == 0
        )
    except FileNotFoundError:
        return False


# Combine ``packaging`` marker with a skip-if guard for missing ``uv``.
pytestmark = [
    pytest.mark.packaging,
    pytest.mark.skipif(not _have_uv(), reason="uv CLI not available"),
]


@pytest.fixture(scope="module")
def built_dist(tmp_path_factory: pytest.TempPathFactory) -> pathlib.Path:
    """Build the sdist + wheel once into a module-scoped temp directory."""
    out_dir = tmp_path_factory.mktemp("dist")
    result = subprocess.run(
        ["uv", "build", "--out-dir", str(out_dir)],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"uv build failed (exit={result.returncode}):\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    return out_dir


def test_uv_build_produces_one_sdist_and_one_wheel(built_dist: pathlib.Path) -> None:
    """``uv build`` must emit exactly one sdist and one wheel."""
    sdists = list(built_dist.glob("pagetomd-*.tar.gz"))
    wheels = list(built_dist.glob("pagetomd-*-py3-none-any.whl"))
    assert len(sdists) == 1, f"expected 1 sdist, got {sdists}"
    assert len(wheels) == 1, f"expected 1 wheel, got {wheels}"


def test_wheel_installs_and_entry_point_runs(
    built_dist: pathlib.Path,
    tmp_path: pathlib.Path,
) -> None:
    """Install the wheel into a fresh venv and run ``pagetomd --version``."""
    wheel = next(built_dist.glob("pagetomd-*-py3-none-any.whl"))

    venv_dir = tmp_path / "venv"
    venv.create(venv_dir, with_pip=True, clear=True, symlinks=(os.name != "nt"))

    # ``Scripts/`` on Windows, ``bin/`` elsewhere.
    bin_dir = venv_dir / ("Scripts" if os.name == "nt" else "bin")
    venv_python = bin_dir / ("python.exe" if os.name == "nt" else "python")
    venv_pagetomd = bin_dir / ("pagetomd.exe" if os.name == "nt" else "pagetomd")

    install = subprocess.run(
        [str(venv_python), "-m", "pip", "install", "--quiet", str(wheel)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert install.returncode == 0, (
        f"pip install failed (exit={install.returncode}):\n"
        f"STDOUT:\n{install.stdout}\nSTDERR:\n{install.stderr}"
    )

    version_check = subprocess.run(
        [str(venv_pagetomd), "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert version_check.returncode == 0, (
        f"pagetomd --version failed (exit={version_check.returncode}):\n"
        f"STDOUT:\n{version_check.stdout}\nSTDERR:\n{version_check.stderr}"
    )
    # Expect a line like ``pagetomd 0.1.0`` or ``pagetomd 0.1.dev0+...``.
    assert re.match(
        r"^pagetomd \S+\s*$",
        version_check.stdout.strip(),
    ), f"unexpected --version output: {version_check.stdout!r}"


def test_sdist_contains_expected_files_and_excludes_tests(
    built_dist: pathlib.Path,
) -> None:
    """Sdist contains required files and excludes ``tests/`` and ``docs/``."""
    sdist = next(built_dist.glob("pagetomd-*.tar.gz"))

    with tarfile.open(sdist, mode="r:gz") as tar:
        # Member names look like ``pagetomd-0.1.dev0+.../README.md`` so
        # strip the leading path component to compare against the
        # project-relative paths we care about.
        members = {
            member.name.split("/", maxsplit=1)[1]
            for member in tar.getmembers()
            if "/" in member.name
        }

    required = {
        "src/pagetomd/__init__.py",
        "LICENSE",
        "README.md",
        "CHANGELOG.md",
    }
    missing = required - members
    assert not missing, f"sdist missing required files: {sorted(missing)}"

    # Anything starting with ``tests/`` or ``docs/`` is a packaging bug.
    bundled_tests = sorted(m for m in members if m.startswith("tests/"))
    bundled_docs = sorted(m for m in members if m.startswith("docs/"))
    assert not bundled_tests, f"sdist should not bundle tests/: {bundled_tests}"
    assert not bundled_docs, f"sdist should not bundle docs/: {bundled_docs}"
