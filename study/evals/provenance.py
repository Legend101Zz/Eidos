"""Read-only checks for a dedicated, frozen evaluation installation."""

import hashlib
import json
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

EVAL_ROOT = Path("/Volumes/Mrigesh SSD/eidos-v1/evals-labs")
MIN_FREE = int(12.5 * 1024**3)
PACKAGES = {"eidos-lab", "eidos-wire", "eidos-worker-sdk", "eidos-pytorch"}
SHA = re.compile(r"[a-f0-9]{40}")


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def disk_guard():
    if shutil.disk_usage("/").free < MIN_FREE:
        raise RuntimeError("internal disk has less than 12.5 GiB free")


def controller_record(repo, *, live):
    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()

    sha = git("rev-parse", "--verify", "HEAD")
    dirty = bool(git("status", "--porcelain"))
    if live and (dirty or not SHA.fullmatch(sha)):
        raise ValueError("live eval requires a clean, committed controller")
    return {"git_sha": sha, "git_dirty": dirty}


def verify_build(repo, home, python, expected, receipt):
    if not isinstance(expected, str) or not SHA.fullmatch(expected):
        raise ValueError("live eval requires an explicit full --expected-build-sha")
    if receipt.get("eidos_commit_sha") != expected:
        raise ValueError("frozen build SHA differs from --expected-build-sha")
    if (
        home.name != "home"
        or home.parent.parent != EVAL_ROOT.resolve()
        or python != home.parent / "venv/bin/python"
        or receipt.get("python") != str(python)
    ):
        raise ValueError("live eval requires a dedicated builder-created eval lab and matching interpreter")
    archive = subprocess.Popen(
        ["git", "-C", str(repo), "archive", expected],
        stdout=subprocess.PIPE,
    )
    with archive.stdout:
        source_digest = hashlib.file_digest(archive.stdout, "sha256").hexdigest()
    if archive.wait() or receipt.get("source_archive_sha256") != source_digest:
        raise ValueError("frozen source archive provenance is missing or differs from the requested commit")
    wheel_root = home.parent / "wheels"
    wheels = {p.name: p for p in sorted(wheel_root.glob("*.whl")) if p.is_file() and not p.is_symlink()}
    names = [name.split("-")[0].replace("_", "-") for name in wheels]
    if len(names) != len(PACKAGES) or set(names) != PACKAGES:
        raise ValueError("frozen wheel inventory does not contain exactly the expected Eidos packages")
    recorded = receipt.get("wheels")
    if not isinstance(recorded, dict) or not wheels or set(wheels) != set(recorded):
        raise ValueError("frozen wheel inventory differs from the build receipt")
    if any(digest(wheel) != recorded[name] for name, wheel in wheels.items()):
        raise ValueError("frozen wheel hash differs from the build receipt")

    inventory = json.loads(
        subprocess.check_output(
            [
                str(python),
                "-I",
                "-c",
                "import importlib.metadata as m, json, sys;"
                "ds={n:m.distribution(n) for n in sys.argv[1:]};"
                "print(json.dumps({'prefix':sys.prefix,'packages':{n:{'version':d.version,"
                "'location':str(d.locate_file('')),'origin':json.loads(d.read_text('direct_url.json') or '{}')}"
                " for n,d in ds.items()}}))",
                *sorted(PACKAGES),
            ],
            text=True,
        )
    )
    venv = home.parent / "venv"
    if Path(inventory["prefix"]).resolve() != venv:
        raise ValueError("eval interpreter prefix differs from the dedicated venv")
    for name, installed in inventory["packages"].items():
        root = Path(installed["location"]).resolve()
        wheel = next(
            (path for path in wheels.values() if path.name.split("-")[0].replace("_", "-") == name), None
        )
        if (
            wheel is None
            or not root.is_relative_to(venv)
            or receipt.get("packages", {}).get(name) != installed["version"]
            or installed["origin"].get("url") != wheel.as_uri()
        ):
            raise ValueError(f"missing or mismatched installation provenance for {name}")
        with zipfile.ZipFile(wheel) as archive:
            for entry in archive.infolist():
                if entry.is_dir() or entry.filename.endswith(".dist-info/RECORD"):
                    continue
                path = (root / entry.filename).resolve()
                if (
                    not path.is_relative_to(root)
                    or not path.is_file()
                    or hashlib.sha256(archive.read(entry)).hexdigest() != digest(path)
                ):
                    raise ValueError(f"installed wheel payload differs for {name}: {entry.filename}")
    return {
        "expected_build_sha": expected,
        "wheel_sha256": recorded,
        "source_archive_sha256": source_digest,
        "verified_packages": sorted(PACKAGES),
    }
