"""Local content-addressed artifact store.

Layout under ``root``: ``blobs/sha256/ab/<hex>`` immutable files, ``staging/`` private temporary
files. Bytes are written to a private temp file, fsynced, hashed and atomically renamed; a blob
is published (made visible to the ledger) only by the command service after ``verify``.
Paths are derived from digests only, so no API can be made to read an arbitrary path.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
from pathlib import Path

_HEX = re.compile(r"^[0-9a-f]{64}$")


class StoreError(Exception):
    pass


class DiskPressure(StoreError):
    """Refuse new bytes when free space is below the configured reserve."""


def normalize(digest: str) -> str:
    d = digest.split(":", 1)[1] if digest.startswith("sha256:") else digest
    if not _HEX.match(d):
        raise StoreError(f"invalid sha256 digest {digest!r}")
    return d


class LocalArtifactStore:
    def __init__(self, root: str | Path, *, min_free_bytes: int = 256 << 20):
        self.root = Path(root)
        self.blobs = self.root / "blobs" / "sha256"
        self.staging = self.root / "staging"
        self.min_free_bytes = min_free_bytes
        for d in (self.blobs, self.staging):
            d.mkdir(parents=True, exist_ok=True)
            os.chmod(d, 0o750)

    def path_for(self, digest: str) -> str:
        d = normalize(digest)
        return str(self.blobs / d[:2] / d)

    def has(self, digest: str, size: int) -> bool:
        p = Path(self.path_for(digest))
        return p.is_file() and not p.is_symlink() and p.stat().st_size == int(size)

    def verify(self, digest: str, size: int) -> bool:
        """Rehash the blob: publication never trusts a size check alone."""
        if not self.has(digest, size):
            return False
        h = hashlib.sha256()
        with open(self.path_for(digest), "rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                h.update(block)
        return h.hexdigest() == normalize(digest)

    def check(self, digest: str, size: int) -> str | None:
        """Why staged content cannot be published, or None when it verifies (size and full rehash).

        ``missing``: no regular file at the digest's path; ``size-changed``: the file's size differs from the
        declared size; ``digest-changed``: the bytes no longer hash to their digest."""
        p = Path(self.path_for(digest))
        if not p.is_file() or p.is_symlink():
            return "missing"
        if p.stat().st_size != int(size):
            return "size-changed"
        return None if self.verify(digest, size) else "digest-changed"

    def check_with_retry(self, digest: str, size: int, *, attempts: int = 3, delay: float = 0.05) -> str | None:
        """``check`` with a short bounded retry: storage that lags the database (a write not yet visible)
        gets a few chances; a persistent failure is returned, never hidden."""
        import time
        reason = self.check(digest, size)
        for i in range(1, attempts):
            if reason is None:
                return None
            time.sleep(delay * i)
            reason = self.check(digest, size)
        return reason

    def free_bytes(self) -> int:
        return shutil.disk_usage(self.root).free

    def _check_space(self, need: int) -> None:
        free = self.free_bytes()
        if free - need < self.min_free_bytes:
            raise DiskPressure(f"refusing {need} bytes: {free} free, reserve {self.min_free_bytes}")

    def _commit_temp(self, tmp: Path, digest: str) -> tuple[str, int]:
        dst = Path(self.path_for(digest))
        size = tmp.stat().st_size
        if dst.exists() and self.check(digest, size) is None:
            tmp.unlink()
            return digest, size
        # Absent, or present but damaged (changed size or bytes): the freshly hashed copy replaces it.
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(tmp, 0o440)
        os.replace(tmp, dst)
        dfd = os.open(dst.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
        return digest, size

    def put_bytes(self, data: bytes) -> tuple[str, int]:
        self._check_space(len(data))
        fd, name = tempfile.mkstemp(dir=self.staging)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        return self._commit_temp(Path(name), hashlib.sha256(data).hexdigest())

    def put_file(self, path: str | Path, *, expected_digest: str | None = None, expected_size: int | None = None) -> tuple[str, int]:
        src = Path(path)
        if src.is_symlink() or not src.is_file():
            raise StoreError(f"refusing non-regular file {src}")
        size = src.stat().st_size
        if expected_size is not None and size != expected_size:
            raise StoreError(f"{src.name}: size {size} != declared {expected_size}")
        self._check_space(size)
        fd, name = tempfile.mkstemp(dir=self.staging)
        h = hashlib.sha256()
        with os.fdopen(fd, "wb") as out, open(src, "rb") as inp:
            for block in iter(lambda: inp.read(1 << 20), b""):
                h.update(block)
                out.write(block)
            out.flush()
            os.fsync(out.fileno())
        digest = h.hexdigest()
        if expected_digest is not None and normalize(expected_digest) != digest:
            Path(name).unlink()
            raise StoreError(f"{src.name}: digest mismatch (declared {expected_digest}, actual {digest})")
        return self._commit_temp(Path(name), digest)

    def sweep_staging(self, older_than_seconds: int = 3600) -> int:
        import time
        n = 0
        cutoff = time.time() - older_than_seconds
        for p in self.staging.iterdir():
            if p.is_file() and p.stat().st_mtime < cutoff:
                p.unlink()
                n += 1
        return n

    def all_digests(self) -> list[str]:
        return [p.name for p in self.blobs.glob("*/*") if p.is_file()]
