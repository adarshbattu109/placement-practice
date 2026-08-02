"""The things that only break once the project is installed rather than run from source.

`data/` and `src/db/schema.sql` are not Python packages, so `packages = [...]` alone would
build a wheel that starts up and then fails on the first query.
"""

import subprocess
import zipfile

from src.constants.filepaths import ROOT

SHIPPED = ("data/questions.json", "src/db/schema.sql", "app/index.html")


def check():
    # check=False so a build failure surfaces as an assertion with uv's own stderr
    # attached, rather than a CalledProcessError that says only "returned 1".
    build = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", "dist"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert build.returncode == 0, build.stderr

    wheel = max((ROOT / "dist").glob("*.whl"), key=lambda p: p.stat().st_mtime)
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
    for path in SHIPPED:
        assert path in names, f"{path} is missing from {wheel.name}"

    # The database is created at runtime; shipping one would ship its accounts.
    leaked = [n for n in names if n.endswith((".db", ".db-wal", ".db-shm", ".env", ".env.local"))]
    assert not leaked, f"{wheel.name} must not carry local state: {leaked}"

    print(f"ok — {wheel.name} ships the bank, schema and frontend, and no local state")


if __name__ == "__main__":
    check()
