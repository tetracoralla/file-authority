"""Build and inspect both distributions, then exercise an installed wheel."""

from __future__ import annotations

import json
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import venv
import zipfile
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    with tempfile.TemporaryDirectory(prefix="file authority package ") as raw:
        temporary = Path(raw)
        distributions = temporary / "dist"
        subprocess.run(
            [sys.executable, "-m", "build", "--outdir", str(distributions)],
            cwd=root,
            check=True,
        )
        wheel, = distributions.glob("*.whl")
        sdist, = distributions.glob("*.tar.gz")
        with zipfile.ZipFile(wheel) as archive:
            names = archive.namelist()
            assert "file_authority/__init__.py" in names
            for legal in ("LICENSE", "NOTICE"):
                assert any(name.endswith("/licenses/" + legal) for name in names)
            assert all(
                name.startswith("file_authority/") or ".dist-info/" in name
                for name in names
            ), names
        with tarfile.open(sdist) as archive:
            members = archive.getmembers()
            assert all(not member.issym() and not member.islnk() for member in members)
            names = [member.name.partition("/")[2] for member in members]
            for forbidden in (".venv/", ".review/", ".task-state/", "__pycache__/"):
                assert not any(forbidden in name for name in names), forbidden
            assert "pyproject.toml" in names
            assert "LICENSE" in names and "NOTICE" in names
        environment = temporary / "installed"
        # Keep the signed interpreter intact on macOS, including uv Python builds.
        venv.EnvBuilder(with_pip=True, symlinks=True).create(environment)
        python = environment / "bin/python"
        subprocess.run(
            [str(python), "-m", "pip", "install", "--no-index", "--no-deps", str(wheel)],
            check=True,
        )
        probe = temporary / "probe.py"
        probe.write_text('''from pathlib import Path
import tempfile
import file_authority
from file_authority import (
    anchor_root, open_input_file, anchor_output_file, create_staging,
    publish_file, FileAuthorityError,
)
assert "installed" in file_authority.__file__
with tempfile.TemporaryDirectory() as raw:
    workspace = Path(raw)
    (workspace / "input.txt").write_bytes(b"original")
    with anchor_root(workspace) as root:
        with open_input_file(root, "input.txt", max_bytes=100) as source:
            (workspace / "input.txt").rename(workspace / "old.txt")
            (workspace / "input.txt").write_bytes(b"replacement")
            assert source.read_bytes() == b"original"
        try:
            open_input_file(root, "../outside", max_bytes=100)
        except FileAuthorityError as error:
            assert error.code == "PATH_FORBIDDEN"
        else:
            raise AssertionError("escape accepted")
        slot = anchor_output_file(root, "result.txt")
        try:
            staged = create_staging(slot.directory, prefix="result")
            with staged.file_object() as output:
                output.write(b"published")
            result = publish_file(staged, slot, overwrite=False)
            assert result.published and result.staging_removed
            assert (workspace / "result.txt").read_bytes() == b"published"
            second = create_staging(slot.directory, prefix="result")
            with second.file_object() as output:
                output.write(b"wrong")
            try:
                publish_file(second, slot, overwrite=False)
            except FileAuthorityError as error:
                assert error.code == "OUTPUT_EXISTS"
            else:
                raise AssertionError("exclusive publication overwrote a result")
            assert (workspace / "result.txt").read_bytes() == b"published"
        finally:
            slot.directory.close()
print("PASS: isolated installed authority, replacement and publication")
''')
        subprocess.run([str(python), "-I", str(probe)], cwd=temporary, check=True)
        print(json.dumps({"package": project["name"], "version": project["version"],
                          "wheel": wheel.name, "sdist": sdist.name, "status": "PASS"}))


if __name__ == "__main__":
    main()
